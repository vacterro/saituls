"""Windows HID transport for FIDO2 authenticators (usage page 0xF1D0).

Plain ctypes over setupapi.dll, hid.dll and kernel32.dll -- no third-party
package. Reads and writes use overlapped I/O so every wait is bounded and a
pending read can be cancelled.

Access rule worth knowing: since Windows 10 1903 only an elevated process may
open a FIDO HID device directly; a medium-integrity process is expected to go
through webauthn.dll instead. The Secure Apps broker runs elevated for exactly
this reason (and for BitLocker and VHDX attach), so :func:`list_devices`
reports the access failure as a capability detail instead of pretending no
key is plugged in.
"""
import ctypes
import os
from ctypes import wintypes

FIDO_USAGE_PAGE = 0xF1D0
FIDO_USAGE = 0x01
HID_REPORT_SIZE = 64

_is_windows = os.name == "nt"


class HidError(Exception):
    """Transport-level failure: open, read, write or timeout."""

    def __init__(self, message, winerror=0):
        Exception.__init__(self, message)
        self.winerror = winerror


class HidTimeout(HidError):
    pass


if _is_windows:
    _setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    _hid = ctypes.WinDLL("hid", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", GUID),
                    ("Flags", wintypes.DWORD), ("Reserved", ctypes.c_void_p)]

    class HIDD_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT),
                    ("ProductID", wintypes.USHORT), ("VersionNumber", wintypes.USHORT)]

    class HIDP_CAPS(ctypes.Structure):
        _fields_ = [("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
                    ("InputReportByteLength", wintypes.USHORT),
                    ("OutputReportByteLength", wintypes.USHORT),
                    ("FeatureReportByteLength", wintypes.USHORT),
                    ("Reserved", wintypes.USHORT * 17),
                    ("NumberLinkCollectionNodes", wintypes.USHORT),
                    ("NumberInputButtonCaps", wintypes.USHORT),
                    ("NumberInputValueCaps", wintypes.USHORT),
                    ("NumberInputDataIndices", wintypes.USHORT),
                    ("NumberOutputButtonCaps", wintypes.USHORT),
                    ("NumberOutputValueCaps", wintypes.USHORT),
                    ("NumberOutputDataIndices", wintypes.USHORT),
                    ("NumberFeatureButtonCaps", wintypes.USHORT),
                    ("NumberFeatureValueCaps", wintypes.USHORT),
                    ("NumberFeatureDataIndices", wintypes.USHORT)]

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    DIGCF_PRESENT = 0x02
    DIGCF_DEVICEINTERFACE = 0x10
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    FILE_FLAG_OVERLAPPED = 0x40000000
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    ERROR_IO_PENDING = 997
    ERROR_NO_MORE_ITEMS = 259
    ERROR_INSUFFICIENT_BUFFER = 122
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 0x102
    HIDP_STATUS_SUCCESS = 0x00110000

    _setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    _setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR,
                                               wintypes.HWND, wintypes.DWORD]
    _setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    _setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    _setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    _setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
    _setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]

    _hid.HidD_GetHidGuid.restype = None
    _hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    _hid.HidD_GetAttributes.restype = wintypes.BOOLEAN
    _hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
    _hid.HidD_GetPreparsedData.restype = wintypes.BOOLEAN
    _hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    _hid.HidD_FreePreparsedData.restype = wintypes.BOOLEAN
    _hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    _hid.HidP_GetCaps.restype = ctypes.c_long
    _hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    _hid.HidD_GetProductString.restype = wintypes.BOOLEAN
    _hid.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]

    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                       wintypes.LPCWSTR]
    _kernel32.ResetEvent.restype = wintypes.BOOL
    _kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.GetOverlappedResult.restype = wintypes.BOOL
    _kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
                                              ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    _kernel32.CancelIoEx.restype = wintypes.BOOL
    _kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]


class HidDescriptor(object):
    """What enumeration learned about one FIDO HID interface. No secrets."""

    __slots__ = ("path", "vendor_id", "product_id", "product_name",
                 "input_report_length", "output_report_length")

    def __init__(self, path, vendor_id=0, product_id=0, product_name="",
                 input_report_length=65, output_report_length=65):
        self.path = path
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.product_name = product_name
        self.input_report_length = input_report_length
        self.output_report_length = output_report_length

    def to_dict(self):
        return {
            "vendor_id": "%04x" % self.vendor_id,
            "product_id": "%04x" % self.product_id,
            "product_name": self.product_name,
        }


class EnumerationResult(object):
    __slots__ = ("devices", "access_denied", "hid_interfaces")

    def __init__(self):
        self.devices = []
        self.access_denied = 0
        self.hid_interfaces = 0


def _open(path, access, overlapped=False):
    flags = FILE_FLAG_OVERLAPPED if overlapped else 0
    handle = _kernel32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                   None, OPEN_EXISTING, flags, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        return None, ctypes.get_last_error()
    return handle, 0


def _interface_paths():
    guid = GUID()
    _hid.HidD_GetHidGuid(ctypes.byref(guid))
    info_set = _setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                              DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if info_set in (None, INVALID_HANDLE_VALUE):
        raise HidError("SetupDiGetClassDevs failed", ctypes.get_last_error())
    paths = []
    try:
        index = 0
        while True:
            data = SP_DEVICE_INTERFACE_DATA()
            data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not _setupapi.SetupDiEnumDeviceInterfaces(info_set, None, ctypes.byref(guid),
                                                         index, ctypes.byref(data)):
                if ctypes.get_last_error() == ERROR_NO_MORE_ITEMS:
                    break
                index += 1
                continue
            index += 1
            required = wintypes.DWORD(0)
            _setupapi.SetupDiGetDeviceInterfaceDetailW(info_set, ctypes.byref(data), None, 0,
                                                       ctypes.byref(required), None)
            if required.value == 0:
                continue
            buffer = ctypes.create_string_buffer(required.value + 4)
            # cbSize is the fixed part of SP_DEVICE_INTERFACE_DETAIL_DATA_W:
            # DWORD + one WCHAR, padded to the pointer alignment.
            fixed = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            ctypes.memmove(buffer, ctypes.byref(wintypes.DWORD(fixed)), 4)
            if not _setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info_set, ctypes.byref(data), buffer, required.value, None, None):
                continue
            paths.append(ctypes.wstring_at(ctypes.addressof(buffer) + 4))
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(info_set)
    return paths


def _describe(path):
    """Caps and attributes, read through a zero-access handle."""
    handle, error = _open(path, 0)
    if handle is None:
        return None, error
    try:
        preparsed = ctypes.c_void_p()
        if not _hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            return None, ctypes.get_last_error()
        try:
            caps = HIDP_CAPS()
            if _hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
                return None, 0
        finally:
            _hid.HidD_FreePreparsedData(preparsed)
        if caps.UsagePage != FIDO_USAGE_PAGE or caps.Usage != FIDO_USAGE:
            return None, 0
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        _hid.HidD_GetAttributes(handle, ctypes.byref(attrs))
        name_buffer = ctypes.create_unicode_buffer(128)
        name = ""
        if _hid.HidD_GetProductString(handle, name_buffer, ctypes.sizeof(name_buffer)):
            name = name_buffer.value
        return HidDescriptor(path, attrs.VendorID, attrs.ProductID, name,
                             caps.InputReportByteLength or 65,
                             caps.OutputReportByteLength or 65), 0
    finally:
        _kernel32.CloseHandle(handle)


def list_devices():
    """Enumerate FIDO HID interfaces. Returns an :class:`EnumerationResult`."""
    result = EnumerationResult()
    if not _is_windows:
        return result
    for path in _interface_paths():
        result.hid_interfaces += 1
        descriptor, _error = _describe(path)
        if descriptor is None:
            continue
        handle, error = _open(path, GENERIC_READ | GENERIC_WRITE, overlapped=True)
        if handle is None:
            if error == 5:
                result.access_denied += 1
            continue
        _kernel32.CloseHandle(handle)
        result.devices.append(descriptor)
    return result


class WindowsHidConnection(object):
    """One open FIDO HID interface. Packets are 64 bytes without report id."""

    packet_size = HID_REPORT_SIZE

    def __init__(self, descriptor):
        self.descriptor = descriptor
        handle, error = _open(descriptor.path, GENERIC_READ | GENERIC_WRITE, overlapped=True)
        if handle is None:
            if error == 5:
                raise HidError("access to the FIDO2 authenticator was denied; the "
                               "Secure Apps broker must run elevated", error)
            raise HidError("could not open the FIDO2 authenticator", error)
        self._handle = handle
        self._event = _kernel32.CreateEventW(None, True, False, None)
        if not self._event:
            _kernel32.CloseHandle(handle)
            raise HidError("CreateEvent failed", ctypes.get_last_error())

    def _io(self, func, buffer, length, timeout_ms):
        overlapped = OVERLAPPED()
        _kernel32.ResetEvent(self._event)
        overlapped.hEvent = self._event
        transferred = wintypes.DWORD(0)
        ok = func(self._handle, buffer, length, ctypes.byref(transferred),
                  ctypes.byref(overlapped))
        if not ok:
            error = ctypes.get_last_error()
            if error != ERROR_IO_PENDING:
                raise HidError("HID I/O failed (winerror %d)" % error, error)
            wait = _kernel32.WaitForSingleObject(self._event, int(timeout_ms))
            if wait == WAIT_TIMEOUT:
                _kernel32.CancelIoEx(self._handle, ctypes.byref(overlapped))
                _kernel32.GetOverlappedResult(self._handle, ctypes.byref(overlapped),
                                              ctypes.byref(transferred), True)
                raise HidTimeout("HID I/O timed out")
            if wait != WAIT_OBJECT_0:
                raise HidError("HID wait failed", ctypes.get_last_error())
            if not _kernel32.GetOverlappedResult(self._handle, ctypes.byref(overlapped),
                                                 ctypes.byref(transferred), False):
                error = ctypes.get_last_error()
                raise HidError("HID I/O failed (winerror %d)" % error, error)
        return transferred.value

    def write_packet(self, packet, timeout=5.0):
        if len(packet) != self.packet_size:
            raise HidError("HID packet must be %d bytes" % self.packet_size)
        length = max(self.descriptor.output_report_length, self.packet_size + 1)
        buffer = ctypes.create_string_buffer(length)
        buffer[0] = b"\x00"
        ctypes.memmove(ctypes.addressof(buffer) + 1, bytes(packet), self.packet_size)
        try:
            self._io(_kernel32.WriteFile, buffer, length, timeout * 1000)
        finally:
            ctypes.memset(buffer, 0, length)

    def read_packet(self, timeout=5.0):
        length = max(self.descriptor.input_report_length, self.packet_size + 1)
        buffer = ctypes.create_string_buffer(length)
        try:
            count = self._io(_kernel32.ReadFile, buffer, length, timeout * 1000)
            if count < self.packet_size + 1:
                raise HidError("short HID report (%d bytes)" % count)
            return bytes(buffer.raw[1:1 + self.packet_size])
        finally:
            ctypes.memset(buffer, 0, length)

    def close(self):
        if getattr(self, "_handle", None):
            _kernel32.CloseHandle(self._handle)
            self._handle = None
        if getattr(self, "_event", None):
            _kernel32.CloseHandle(self._event)
            self._event = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
