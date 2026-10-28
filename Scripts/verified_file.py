"""Windows handle-based deletion; unsupported identities fail closed.

The kept copy and candidate stay open without write/delete sharing from the
final digest through disposition. SetFileInformationByHandle targets that
verified object, never a subsequently resolved pathname.
"""
import ctypes
from ctypes import wintypes as w
from contextlib import contextmanager
import hashlib
import os
import stat


class FileInfo(ctypes.Structure):
    _fields_ = [("attributes", w.DWORD), ("created", w.FILETIME),
                ("accessed", w.FILETIME), ("written", w.FILETIME),
                ("volume", w.DWORD), ("size_high", w.DWORD),
                ("size_low", w.DWORD), ("links", w.DWORD),
                ("index_high", w.DWORD), ("index_low", w.DWORD)]


def kernel():
    if os.name != "nt":
        raise OSError("verified permanent deletion requires Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, result, params in [
        ("CreateFileW", w.HANDLE, [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]),
        ("CloseHandle", w.BOOL, [w.HANDLE]),
        ("GetFileInformationByHandle", w.BOOL, [w.HANDLE, ctypes.POINTER(FileInfo)]),
        ("ReadFile", w.BOOL, [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), w.LPVOID]),
        ("SetFileInformationByHandle", w.BOOL, [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]),
        ("GetVolumeInformationByHandleW", w.BOOL, [w.HANDLE, w.LPWSTR, w.DWORD,
         ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), w.LPWSTR, w.DWORD]),
        ("GetFinalPathNameByHandleW", w.DWORD, [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]),
    ]:
        method = getattr(api, name)
        method.restype, method.argtypes = result, params
    return api


def checked(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def confined_directories(scope, directory):
    """Pin all Windows ancestors and reject every reparse component."""
    scope, directory = os.path.abspath(scope), os.path.abspath(directory)
    if os.path.commonpath([scope, directory]) != scope:
        raise OSError(f"outside selected root: {directory}")
    components = []
    current = directory
    while True:
        components.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    handles = []
    api = kernel() if os.name == "nt" else None
    try:
        for current in reversed(components):
            if api:
                handle = api.CreateFileW(current, 0x80, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
                handles.append(handle)
                info = FileInfo()
                checked(api.GetFileInformationByHandle(handle, ctypes.byref(info)))
                if info.attributes & 0x400 or not info.attributes & 0x10:
                    raise OSError(f"skipped reparse or non-directory: {current}")
            else:
                info = os.lstat(current)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise OSError(f"skipped link or non-directory: {current}")
        if os.path.commonpath([os.path.realpath(scope), os.path.realpath(directory)]) != os.path.realpath(scope):
            raise OSError(f"resolved path outside selected root: {directory}")
        yield
    finally:
        for handle in reversed(handles):
            checked(api.CloseHandle(handle))


@contextmanager
def held_file(path, delete=False):
    api = kernel()
    # OPEN_EXISTING, OPEN_REPARSE_POINT; no write/delete sharing.
    handle = api.CreateFileW(os.path.abspath(path), 0x80000000 | (0x10000 if delete else 0),
                             1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = FileInfo()
        checked(api.GetFileInformationByHandle(handle, ctypes.byref(info)))
        if info.attributes & (0x400 | 0x10):
            raise OSError("reparse points and directories cannot be duplicate candidates")
        fs = ctypes.create_unicode_buffer(64)
        checked(api.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, fs, len(fs)))
        final = ctypes.create_unicode_buffer(32768)
        size = api.GetFinalPathNameByHandleW(handle, final, len(final), 0)
        checked(size)
        if size >= len(final) or final.value.upper().startswith("\\\\?\\UNC\\"):
            raise OSError("remote or unresolved file identity is unsupported")
        identity = (info.volume, info.index_high, info.index_low)
        if fs.value != "NTFS" or not any(identity[1:]):
            raise OSError("stable deletion is supported only on local NTFS with a nonzero file ID")
        yield api, handle, info, identity
    finally:
        checked(api.CloseHandle(handle))


def digest_handle(api, handle):
    digest = hashlib.sha256()
    block = ctypes.create_string_buffer(65536)
    count = w.DWORD()
    while True:
        checked(api.ReadFile(handle, block, len(block), ctypes.byref(count), None))
        if not count.value:
            return digest.hexdigest()
        digest.update(block.raw[:count.value])


def delete_verified_duplicate(path, kept, expected, scope=None):
    """Return deleted bytes, or raise without deleting an unverified identity."""
    def _execute():
        with held_file(kept) as (keep_api, keep_handle, _, keep_id):
            with held_file(path, delete=True) as (api, handle, info, identity):
                if identity == keep_id:
                    raise OSError("candidate and keeper name the same file identity")
                if digest_handle(keep_api, keep_handle) != expected:
                    raise OSError("kept copy changed")
                if digest_handle(api, handle) != expected:
                    raise OSError("candidate changed")
                # FILE_DISPOSITION_INFO contains BOOLEAN (one byte), not Win32 BOOL.
                disposition = ctypes.c_ubyte(1)
                checked(api.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), 1))
                return (info.size_high << 32) | info.size_low

    if scope is not None:
        with confined_directories(scope, os.path.dirname(os.path.abspath(kept))):
            with confined_directories(scope, os.path.dirname(os.path.abspath(path))):
                return _execute()
    return _execute()
