"""Read/send keys to the explicitly recorded disposable Windows TUI console."""
import argparse
import ctypes as c
import json
import sys
from ctypes import wintypes as w
from pathlib import Path


class Coord(c.Structure):
    _fields_ = [('X', w.SHORT), ('Y', w.SHORT)]


class Rect(c.Structure):
    _fields_ = [('Left', w.SHORT), ('Top', w.SHORT), ('Right', w.SHORT), ('Bottom', w.SHORT)]


class Info(c.Structure):
    _fields_ = [('size', Coord), ('cursor', Coord), ('attributes', w.WORD),
                ('window', Rect), ('maximum', Coord)]


class Key(c.Structure):
    _fields_ = [('down', w.BOOL), ('repeat', w.WORD), ('vk', w.WORD), ('scan', w.WORD),
                ('char', w.WCHAR), ('control', w.DWORD)]


class Event(c.Union):
    _fields_ = [('key', Key), ('padding', c.c_byte * 16)]


class Record(c.Structure):
    _fields_ = [('type', w.WORD), ('event', Event)]


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session', type=Path, required=True)
    p.add_argument('--text')
    p.add_argument('--steer', action='store_true')
    p.add_argument('--snapshot', type=Path)
    args = p.parse_args()
    pid = json.loads(args.session.read_text())['pid']
    k = c.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.GetConsoleScreenBufferInfo.argtypes = [w.HANDLE, c.POINTER(Info)]
    k.ReadConsoleOutputCharacterW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, Coord, c.POINTER(w.DWORD)]
    k.WriteConsoleInputW.argtypes = [w.HANDLE, c.POINTER(Record), w.DWORD, c.POINTER(w.DWORD)]
    k.CloseHandle.argtypes = [w.HANDLE]
    k.FreeConsole()
    if not k.AttachConsole(pid):
        raise c.WinError(c.get_last_error())
    try:
        if args.text is not None:
            handle = k.CreateFileW('CONIN$', 0xC0000000, 3, None, 3, 0, None)
            records = []
            for char in args.text:
                for down in (True, False):
                    records.append(Record(1, Event(key=Key(down, 1, 0, 0, char, 0))))
            for down in (True, False):
                records.append(Record(1, Event(key=Key(down, 1, 0x53 if args.steer else 13,
                    0, 's' if args.steer else '\r', 10 if args.steer else 0))))
            written = w.DWORD()
            if not k.WriteConsoleInputW(handle, (Record * len(records))(*records), len(records), c.byref(written)):
                raise c.WinError(c.get_last_error())
            k.CloseHandle(handle)
            print(f'KEYS_SENT {written.value} records to pid={pid}', flush=True)
        if args.snapshot:
            handle = k.CreateFileW('CONOUT$', 0x80000000, 3, None, 3, 0, None)
            info = Info()
            if not k.GetConsoleScreenBufferInfo(handle, c.byref(info)):
                raise c.WinError(c.get_last_error())
            count = info.size.X * (info.window.Bottom - info.window.Top + 1)
            buffer = c.create_unicode_buffer(count + 1)
            read = w.DWORD()
            if not k.ReadConsoleOutputCharacterW(handle, buffer, count, Coord(0, info.window.Top), c.byref(read)):
                raise c.WinError(c.get_last_error())
            text = '\n'.join(buffer.value[n:n + info.size.X].rstrip() for n in range(0, read.value, info.size.X))
            args.snapshot.write_text(text, encoding='utf-8')
            k.CloseHandle(handle)
            print(text, flush=True)
    finally:
        k.FreeConsole()


if __name__ == '__main__':
    main()
