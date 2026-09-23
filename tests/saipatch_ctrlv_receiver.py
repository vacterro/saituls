"""Ctrl+V KEY_EVENT driver control (wave clauses 8-9).

A disposable console receiver that parses raw Windows input records the way a
key-aware host parser sees them. The driver's send_ctrl_v(pid) must arrive as
the semantic Ctrl+V sequence:

    VK_CONTROL(0x11) down
    VK_V(0x56) down with LEFT_CTRL_PRESSED and char 0x16
    VK_V up (still LEFT_CTRL_PRESSED)
    VK_CONTROL up (no state)

This control PROVES the injector before the real TUI is blamed. It records
ONLY vk codes, key-down booleans, control-state bits and the integer char
code -- never text, never clipboard content.

Usage:
  python tests/saipatch_ctrlv_receiver.py --out FILE   (own console; driver injects)
  python tests/saipatch_ctrlv_receiver.py --self-test   (runs the oracle standalone)
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


kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, wt.DWORD]
kernel32.SetConsoleMode.restype = wt.BOOL
kernel32.ReadConsoleInputW.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(INPUT_RECORD), wt.DWORD, ctypes.POINTER(wt.DWORD),
]
kernel32.ReadConsoleInputW.restype = wt.BOOL


def read_key_records(handle, count, timeout):
    """Read up to `count` KEY_EVENT records (skipping other event types)."""
    records = []
    deadline = time.monotonic() + timeout
    while len(records) < count and time.monotonic() < deadline:
        batch = (INPUT_RECORD * 16)()
        got = wt.DWORD(0)
        if not kernel32.ReadConsoleInputW(handle, batch, 16, ctypes.byref(got)):
            time.sleep(0.05)
            continue
        for i in range(got.value):
            rec = batch[i]
            if rec.EventType != 1:
                continue
            k = rec.Event.KeyEvent
            records.append({
                "vk": int(k.wVirtualKeyCode), "down": bool(k.bKeyDown),
                "ctrl": bool(k.dwControlKeyState & 0x0008),
                "char": ord(k.uChar.UnicodeChar) if k.uChar.UnicodeChar else 0,
            })
    return records


def classify_ctrl_v(records):
    """Semantic Ctrl+V oracle. Returns (ok, reason)."""
    if len(records) < 4:
        return False, f"expected>=4 records, got {len(records)}"
    first, second, third, fourth = records[0], records[1], records[2], records[3]
    if not (first["vk"] == 0x11 and first["down"] is True):
        return False, "record0 != VK_CONTROL down"
    if not (second["vk"] == 0x56 and second["down"] is True and second["ctrl"] and second["char"] == 0x16):
        return False, f"record1 != VK_V down ctrl+0x16 (vk={second['vk']} ctrl={second['ctrl']} char={hex(second['char'])})"
    if not (third["vk"] == 0x56 and third["down"] is False):
        return False, "record2 != VK_V up"
    if not (fourth["vk"] == 0x11 and fourth["down"] is False):
        return False, "record3 != VK_CONTROL up"
    return True, "semantic ctrl+v: ctrl down, v down(0x16,LEFT_CTRL), v up, ctrl up"


def self_test():
    ok, reason = classify_ctrl_v([
        {"vk": 0x11, "down": True, "ctrl": True, "char": 0},
        {"vk": 0x56, "down": True, "ctrl": True, "char": 0x16},
        {"vk": 0x56, "down": False, "ctrl": True, "char": 0x16},
        {"vk": 0x11, "down": False, "ctrl": False, "char": 0},
    ])
    assert ok, reason
    bad, why = classify_ctrl_v([
        {"vk": 0x56, "down": True, "ctrl": False, "char": 0x16},  # no ctrl down first
        {"vk": 0x56, "down": False, "ctrl": False, "char": 0x16},
    ])
    assert bad is False and why
    print(json.dumps({"self_test": "PASS", "reason": reason}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    handle = kernel32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE, 3, None, OPEN_EXISTING, 0, None)
    if (not handle) or handle == INVALID_HANDLE_VALUE:
        raise RuntimeError(f"CreateFileW(CONIN$) failed: {ctypes.get_last_error()}")
    try:
        kernel32.SetConsoleMode(handle, 0)
        out = args.out.resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".ready").write_text(json.dumps({"ready": True, "pid": os.getpid()}), encoding="utf-8")
        records = read_key_records(handle, 4, args.timeout)
        ok, reason = classify_ctrl_v(records)
        out.write_text(json.dumps({
            "records": records,  # vk/down/ctrl/int-code only; no text
            "semantic_ctrl_v": ok,
            "reason": reason,
        }, indent=2), encoding="utf-8")
        return 0 if ok else 1
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
