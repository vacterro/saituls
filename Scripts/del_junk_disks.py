"""DEL JUNK all-disks discovery: which volumes an ALL DISKS run may touch.

The single-target worker asks the user where to clean. ALL DISKS has no such
witness, so eligibility is decided here, once, from the Windows drive-type API
(`GetLogicalDrives` / `GetDriveTypeW` / `GetVolumeInformationW` /
`GetDiskFreeSpaceExW`) rather than from localized shell output.

Eligible by default: local FIXED volumes that are mounted, accessible and
writable. Deliberately excluded: network shares and disconnected mappings
(DRIVE_REMOTE), optical drives (DRIVE_CDROM), RAM disks (DRIVE_RAMDISK),
removable/USB media (DRIVE_REMOVABLE), unmounted letters (DRIVE_NO_ROOT_DIR),
read-only volumes and anything that fails to answer. Every exclusion is
returned with a reason instead of being dropped silently.

The Windows system drive is detected at runtime (`GetSystemWindowsDirectory`,
then SystemRoot/windir/SystemDrive) and never hardcoded. It stays eligible, but
ALL DISKS is SAFE_DISK-only, so the aggressive project rules can never reach
it.

Nothing here deletes, scans or locks anything. Self-check:
``python Scripts\\del_junk_disks.py`` -> 0 on success.
"""

import ctypes
import os
import sys

# GetDriveTypeW return values.
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

DRIVE_TYPE_NAMES = {
    DRIVE_UNKNOWN: "UNKNOWN",
    DRIVE_NO_ROOT_DIR: "NOT_MOUNTED",
    DRIVE_REMOVABLE: "REMOVABLE",
    DRIVE_FIXED: "FIXED",
    DRIVE_REMOTE: "NETWORK",
    DRIVE_CDROM: "OPTICAL",
    DRIVE_RAMDISK: "RAMDISK",
}

# The whole allowlist. Widening it is a policy decision, not a detail.
ELIGIBLE_DRIVE_TYPES = frozenset({DRIVE_FIXED})

FILE_READ_ONLY_VOLUME = 0x00080000
SEM_FAILCRITICALERRORS = 0x0001

# Per-drive outcome of an ALL DISKS run. A batch reports one of these per drive
# and never summarizes a cancelled or partial pass as CLEANED.
CLEANED = "CLEANED"
NOTHING_TO_DO = "NOTHING_TO_DO"
SKIPPED = "SKIPPED"
PARTIAL = "PARTIAL"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
RESULT_STATES = (CLEANED, NOTHING_TO_DO, SKIPPED, PARTIAL, FAILED, CANCELLED)


def _root_of(path):
    """The volume root a path lives on, as ``X:\\`` (or ``/`` off Windows)."""
    drive, _tail = os.path.splitdrive(os.path.abspath(path))
    return (drive + os.sep) if drive else os.sep


def _normalize(root):
    return os.path.normcase(os.path.abspath(root))


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _native_windows_directory():
    try:
        buf = ctypes.create_unicode_buffer(320)
        if _kernel32().GetSystemWindowsDirectoryW(buf, len(buf)):
            return buf.value
    except (AttributeError, OSError, ValueError):
        pass
    return ""


def system_drive_root(environ=None):
    """Root of the volume Windows booted from. Never assume ``C:``."""
    if environ is None and os.name == "nt":
        native = _native_windows_directory()
        if native:
            return _root_of(native)
    env = os.environ if environ is None else environ
    for name in ("SystemRoot", "windir", "SystemDrive"):
        value = env.get(name)
        if value:
            return _root_of(value)
    return _root_of(os.sep)


def _probe_drive(kernel32, root):
    """One drive letter described by the API, never by its name or label."""
    drive_type = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
    flags = ctypes.c_uint32(0)
    serial = ctypes.c_uint32(0)
    max_component = ctypes.c_uint32(0)
    volume_name = ctypes.create_unicode_buffer(261)
    file_system = ctypes.create_unicode_buffer(261)
    ready = bool(kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), volume_name, len(volume_name),
        ctypes.byref(serial), ctypes.byref(max_component), ctypes.byref(flags),
        file_system, len(file_system)))
    total_bytes = free_bytes = None
    if ready:
        free_to_caller = ctypes.c_ulonglong(0)
        capacity = ctypes.c_ulonglong(0)
        total_free = ctypes.c_ulonglong(0)
        if kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root), ctypes.byref(free_to_caller),
                ctypes.byref(capacity), ctypes.byref(total_free)):
            total_bytes = capacity.value
            free_bytes = free_to_caller.value
        else:
            # A volume that cannot report its size cannot be measured before or
            # after a delete, so it is not a target.
            ready = False
    return {
        "root": root,
        "drive_type": drive_type,
        "drive_type_name": DRIVE_TYPE_NAMES.get(drive_type, "UNKNOWN"),
        "ready": ready,
        "writable": bool(ready and not (flags.value & FILE_READ_ONLY_VOLUME)),
        "file_system": file_system.value if ready else "",
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
    }


def enumerate_windows_drives():
    """Every mounted drive letter, described by the drive-type API."""
    if os.name != "nt":
        return []
    kernel32 = _kernel32()
    # An empty optical/removable slot must not raise a modal "no disk" dialog
    # behind a tray-launched worker.
    previous = kernel32.SetErrorMode(SEM_FAILCRITICALERRORS)
    try:
        mask = kernel32.GetLogicalDrives()
        records = []
        for index in range(26):
            if mask & (1 << index):
                records.append(
                    _probe_drive(kernel32, chr(ord("A") + index) + ":" + os.sep))
        return records
    finally:
        kernel32.SetErrorMode(previous)


def ineligibility(record, exists=None):
    """Why this volume is not an ALL DISKS target, or '' when it is one."""
    exists = os.path.isdir if exists is None else exists
    drive_type = record.get("drive_type", DRIVE_UNKNOWN)
    if drive_type not in ELIGIBLE_DRIVE_TYPES:
        return "not a local fixed disk (%s)" % DRIVE_TYPE_NAMES.get(
            drive_type, "UNKNOWN")
    if not record.get("ready"):
        return "not ready / inaccessible"
    if not record.get("writable"):
        return "read-only volume"
    if not exists(record.get("root", "")):
        return "root is not accessible"
    return ""


def eligible_drives(enumerator=None, system_root=None, exists=None):
    """``(targets, skipped)`` for an ALL DISKS run, decided at execution time.

    ``targets`` carry root, drive type, ready/writable status, capacity and the
    free space measured before the scan. ``skipped`` carry the same identity
    plus an explicit reason -- an excluded or vanished drive is reported, never
    hidden, and never a reason to abandon the other drives.
    """
    records = (enumerate_windows_drives if enumerator is None else enumerator)()
    system = _normalize(system_drive_root() if system_root is None else system_root)
    targets = []
    skipped = []
    for raw in records:
        record = dict(raw)
        record.setdefault("root", "")
        record.setdefault("drive_type", DRIVE_UNKNOWN)
        record.setdefault(
            "drive_type_name",
            DRIVE_TYPE_NAMES.get(record["drive_type"], "UNKNOWN"))
        record["is_system"] = bool(record["root"]) and (
            _normalize(record["root"]) == system)
        reason = ineligibility(record, exists=exists)
        if reason:
            record["result"] = SKIPPED
            record["reason"] = reason
            skipped.append(record)
        else:
            targets.append(record)
    targets.sort(key=lambda item: item["root"].upper())
    skipped.sort(key=lambda item: item["root"].upper())
    return targets, skipped


if __name__ == "__main__":
    ok = True

    def check(name, cond, detail=""):
        global ok
        print(("PASS  " if cond else "FAIL  ") + name + ("  " + detail if detail else ""))
        ok = ok and cond

    fake = [
        {"root": "Q:\\", "drive_type": DRIVE_FIXED, "ready": True, "writable": True,
         "total_bytes": 100, "free_bytes": 50},
        {"root": "R:\\", "drive_type": DRIVE_FIXED, "ready": True, "writable": True,
         "total_bytes": 200, "free_bytes": 20},
        {"root": "N:\\", "drive_type": DRIVE_REMOTE, "ready": True, "writable": True},
        {"root": "D:\\", "drive_type": DRIVE_CDROM, "ready": True, "writable": False},
        {"root": "U:\\", "drive_type": DRIVE_REMOVABLE, "ready": True, "writable": True},
        {"root": "M:\\", "drive_type": DRIVE_RAMDISK, "ready": True, "writable": True},
        {"root": "Z:\\", "drive_type": DRIVE_NO_ROOT_DIR, "ready": False, "writable": False},
        {"root": "F:\\", "drive_type": DRIVE_FIXED, "ready": False, "writable": False},
        {"root": "W:\\", "drive_type": DRIVE_FIXED, "ready": True, "writable": False},
    ]
    targets, skipped = eligible_drives(
        enumerator=lambda: fake, system_root="Q:\\", exists=lambda root: True)
    roots = [item["root"] for item in targets]
    check("only fixed writable ready volumes are targets", roots == ["Q:\\", "R:\\"], repr(roots))
    check("every excluded volume carries a reason",
          all(item.get("reason") for item in skipped) and len(skipped) == 7,
          "%d skipped" % len(skipped))
    check("network drives are excluded",
          "NETWORK" in ineligibility({"root": "N:\\", "drive_type": DRIVE_REMOTE}))
    check("the system drive is detected, not assumed",
          system_drive_root({"SystemRoot": "E:\\Windows"}) == "E:" + os.sep,
          system_drive_root({"SystemRoot": "E:\\Windows"}))
    check("the system volume is flagged for the caller",
          targets[0]["is_system"] and not targets[1]["is_system"])
    check("zero eligible volumes is an empty plan, not an error",
          eligible_drives(enumerator=lambda: [], system_root="Q:\\") == ([], []))
    if os.name == "nt":
        live, live_skipped = eligible_drives()
        check("the live machine reports its own fixed disks", bool(live),
              "%d target(s), %d skipped" % (len(live), len(live_skipped)))
        check("the live system drive is one of the discovered volumes",
              any(item["is_system"] for item in live) or not live,
              system_drive_root())

    print("---")
    print("PASS (0 failures)" if ok else "FAILED")
    sys.exit(0 if ok else 1)
