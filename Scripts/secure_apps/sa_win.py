"""Windows primitives for SAITULS Secure Apps (ctypes, no third-party package).

Grouped by the job each one does for the broker:

* **tokens** -- is this process elevated, is UAC on, what token does the
  interactive shell run with (so a protected application can be started with
  the user's normal rights from an elevated broker);
* **processes and jobs** -- create a process suspended, put it in a job
  object before it runs a single instruction, and ask the job which
  processes are still alive. A job is the only reliable answer to "did the
  whole Electron tree exit?": it keeps counting children whose parent is
  gone;
* **virtual disks** -- create, open, attach and detach a VHDX through
  virtdisk.dll. Attachments default to the lifetime of the broker's handle,
  so a broker that dies cannot leave a decrypted volume attached behind it;
* **volumes** -- find the volumes on a disk, their mount points, set and
  delete a directory mount point, and take an exclusive volume lock. The
  exclusive lock is how the broker proves no handle is open on the vault
  before it detaches anything;
* **session plumbing** -- foreground window owner, WM_CLOSE to a tree,
  shutdown block reasons.

Nothing here ever sees key material.
"""
import ctypes
import os
import subprocess
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"


class WinError(OSError):
    """A Win32 call failed. ``winerror`` carries the system error code."""

    def __init__(self, what, code=None):
        code = ctypes.get_last_error() if code is None else code
        OSError.__init__(self, "%s failed (winerror %d)" % (what, code))
        self.winerror = code
        self.what = what


ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_NOT_READY = 21
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_MORE_DATA = 234
ERROR_NO_MORE_FILES = 18
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_DIR_NOT_EMPTY = 145
ERROR_NOT_A_REPARSE_POINT = 4390

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

if IS_WINDOWS:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    try:
        virtdisk = ctypes.WinDLL("virtdisk", use_last_error=True)
    except OSError:
        virtdisk = None

    def _fn(dll, name, restype, *argtypes):
        func = getattr(dll, name)
        func.restype = restype
        func.argtypes = list(argtypes)
        return func

    HANDLE = wintypes.HANDLE
    PHANDLE = ctypes.POINTER(wintypes.HANDLE)
    DWORD = wintypes.DWORD
    BOOL = wintypes.BOOL
    LPVOID = ctypes.c_void_p

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    # ---------------------------------------------------------- kernel32
    GetCurrentProcess = _fn(kernel32, "GetCurrentProcess", HANDLE)
    CloseHandle = _fn(kernel32, "CloseHandle", BOOL, HANDLE)
    OpenProcess = _fn(kernel32, "OpenProcess", HANDLE, DWORD, BOOL, DWORD)
    ResumeThread = _fn(kernel32, "ResumeThread", DWORD, HANDLE)
    TerminateProcess = _fn(kernel32, "TerminateProcess", BOOL, HANDLE, wintypes.UINT)
    WaitForSingleObject = _fn(kernel32, "WaitForSingleObject", DWORD, HANDLE, DWORD)
    GetExitCodeProcess = _fn(kernel32, "GetExitCodeProcess", BOOL, HANDLE, ctypes.POINTER(DWORD))
    QueryFullProcessImageNameW = _fn(kernel32, "QueryFullProcessImageNameW", BOOL, HANDLE,
                                     DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD))
    CreateJobObjectW = _fn(kernel32, "CreateJobObjectW", HANDLE, LPVOID, wintypes.LPCWSTR)
    SetInformationJobObject = _fn(kernel32, "SetInformationJobObject", BOOL, HANDLE,
                                  ctypes.c_int, LPVOID, DWORD)
    QueryInformationJobObject = _fn(kernel32, "QueryInformationJobObject", BOOL, HANDLE,
                                    ctypes.c_int, LPVOID, DWORD, ctypes.POINTER(DWORD))
    AssignProcessToJobObject = _fn(kernel32, "AssignProcessToJobObject", BOOL, HANDLE, HANDLE)
    TerminateJobObject = _fn(kernel32, "TerminateJobObject", BOOL, HANDLE, wintypes.UINT)
    CreateFileW = _fn(kernel32, "CreateFileW", HANDLE, wintypes.LPCWSTR, DWORD, DWORD, LPVOID,
                      DWORD, DWORD, HANDLE)
    DeviceIoControl = _fn(kernel32, "DeviceIoControl", BOOL, HANDLE, DWORD, LPVOID, DWORD,
                          LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID)
    FlushFileBuffers = _fn(kernel32, "FlushFileBuffers", BOOL, HANDLE)
    GetVolumeNameForVolumeMountPointW = _fn(kernel32, "GetVolumeNameForVolumeMountPointW",
                                            BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, DWORD)
    SetVolumeMountPointW = _fn(kernel32, "SetVolumeMountPointW", BOOL, wintypes.LPCWSTR,
                               wintypes.LPCWSTR)
    DeleteVolumeMountPointW = _fn(kernel32, "DeleteVolumeMountPointW", BOOL, wintypes.LPCWSTR)
    FindFirstVolumeW = _fn(kernel32, "FindFirstVolumeW", HANDLE, wintypes.LPWSTR, DWORD)
    FindNextVolumeW = _fn(kernel32, "FindNextVolumeW", BOOL, HANDLE, wintypes.LPWSTR, DWORD)
    FindVolumeClose = _fn(kernel32, "FindVolumeClose", BOOL, HANDLE)
    GetVolumePathNamesForVolumeNameW = _fn(kernel32, "GetVolumePathNamesForVolumeNameW", BOOL,
                                           wintypes.LPCWSTR, LPVOID, DWORD,
                                           ctypes.POINTER(DWORD))
    GetFileAttributesW = _fn(kernel32, "GetFileAttributesW", DWORD, wintypes.LPCWSTR)
    SetFileAttributesW = _fn(kernel32, "SetFileAttributesW", BOOL, wintypes.LPCWSTR, DWORD)

    # ---------------------------------------------------------- advapi32
    OpenProcessToken = _fn(advapi32, "OpenProcessToken", BOOL, HANDLE, DWORD, PHANDLE)
    GetTokenInformation = _fn(advapi32, "GetTokenInformation", BOOL, HANDLE, ctypes.c_int,
                              LPVOID, DWORD, ctypes.POINTER(DWORD))
    DuplicateTokenEx = _fn(advapi32, "DuplicateTokenEx", BOOL, HANDLE, DWORD, LPVOID,
                           ctypes.c_int, ctypes.c_int, PHANDLE)
    LookupPrivilegeValueW = _fn(advapi32, "LookupPrivilegeValueW", BOOL, wintypes.LPCWSTR,
                                wintypes.LPCWSTR, LPVOID)
    AdjustTokenPrivileges = _fn(advapi32, "AdjustTokenPrivileges", BOOL, HANDLE, BOOL, LPVOID,
                                DWORD, LPVOID, LPVOID)
    CreateProcessWithTokenW = _fn(advapi32, "CreateProcessWithTokenW", BOOL, HANDLE, DWORD,
                                  wintypes.LPCWSTR, wintypes.LPWSTR, DWORD, LPVOID,
                                  wintypes.LPCWSTR, LPVOID, LPVOID)

    # ------------------------------------------------------------ user32
    GetShellWindow = _fn(user32, "GetShellWindow", wintypes.HWND)
    GetForegroundWindow = _fn(user32, "GetForegroundWindow", wintypes.HWND)
    GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", DWORD, wintypes.HWND,
                                   ctypes.POINTER(DWORD))
    IsWindowVisible = _fn(user32, "IsWindowVisible", BOOL, wintypes.HWND)
    PostMessageW = _fn(user32, "PostMessageW", BOOL, wintypes.HWND, wintypes.UINT,
                       wintypes.WPARAM, wintypes.LPARAM)
    WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, wintypes.HWND, wintypes.LPARAM)
    EnumWindows = _fn(user32, "EnumWindows", BOOL, WNDENUMPROC, wintypes.LPARAM)
    ShutdownBlockReasonCreate = _fn(user32, "ShutdownBlockReasonCreate", BOOL, wintypes.HWND,
                                    wintypes.LPCWSTR)
    ShutdownBlockReasonDestroy = _fn(user32, "ShutdownBlockReasonDestroy", BOOL, wintypes.HWND)

    CreateEnvironmentBlock = _fn(userenv, "CreateEnvironmentBlock", BOOL,
                                 ctypes.POINTER(LPVOID), HANDLE, BOOL)
    DestroyEnvironmentBlock = _fn(userenv, "DestroyEnvironmentBlock", BOOL, LPVOID)

    if virtdisk is not None:
        CreateVirtualDisk = _fn(virtdisk, "CreateVirtualDisk", DWORD, LPVOID, wintypes.LPCWSTR,
                                DWORD, LPVOID, DWORD, wintypes.ULONG, LPVOID, LPVOID, PHANDLE)
        OpenVirtualDisk = _fn(virtdisk, "OpenVirtualDisk", DWORD, LPVOID, wintypes.LPCWSTR,
                              DWORD, DWORD, LPVOID, PHANDLE)
        AttachVirtualDisk = _fn(virtdisk, "AttachVirtualDisk", DWORD, HANDLE, LPVOID, DWORD,
                                wintypes.ULONG, LPVOID, LPVOID)
        DetachVirtualDisk = _fn(virtdisk, "DetachVirtualDisk", DWORD, HANDLE, DWORD,
                                wintypes.ULONG)
        GetVirtualDiskPhysicalPath = _fn(virtdisk, "GetVirtualDiskPhysicalPath", DWORD, HANDLE,
                                         ctypes.POINTER(wintypes.ULONG), wintypes.LPWSTR)
        GetVirtualDiskInformation = _fn(virtdisk, "GetVirtualDiskInformation", DWORD, HANDLE,
                                        ctypes.POINTER(wintypes.ULONG), LPVOID,
                                        ctypes.POINTER(wintypes.ULONG))


def _require_windows():
    if not IS_WINDOWS:
        raise WinError("Windows API", 50)


def close_handle(handle):
    if handle and handle != INVALID_HANDLE_VALUE:
        CloseHandle(handle)


# ================================================================ tokens
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
SYNCHRONIZE = 0x00100000
TokenElevationType = 18
TokenElevation = 20
SecurityImpersonation = 2
TokenPrimary = 1


def _token_elevated(token):
    value = DWORD(0)
    size = DWORD(0)
    if not GetTokenInformation(token, TokenElevation, ctypes.byref(value), ctypes.sizeof(value),
                               ctypes.byref(size)):
        raise WinError("GetTokenInformation(TokenElevation)")
    return bool(value.value)


def process_is_elevated():
    """True when this process holds an elevated (full administrator) token."""
    if not IS_WINDOWS:
        return False
    token = HANDLE()
    if not OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise WinError("OpenProcessToken")
    try:
        return _token_elevated(token)
    finally:
        close_handle(token)


def uac_enabled():
    """Whether User Account Control is enabled (EnableLUA). Unknown reads as True."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as key:
            value, _kind = winreg.QueryValueEx(key, "EnableLUA")
            return bool(value)
    except OSError:
        return True


def shell_process_id():
    hwnd = GetShellWindow()
    if not hwnd:
        return 0
    pid = DWORD(0)
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


class ShellToken(object):
    """A primary token duplicated from the interactive shell (Explorer)."""

    def __init__(self, handle, elevated, shell_pid):
        self.handle = handle
        self.elevated = elevated
        self.shell_pid = shell_pid

    def close(self):
        close_handle(self.handle)
        self.handle = None


def open_shell_token():
    """Duplicate Explorer's token. Returns None when there is no shell."""
    _require_windows()
    pid = shell_process_id()
    if not pid:
        return None
    process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        raise WinError("OpenProcess(shell)")
    source = HANDLE()
    try:
        if not OpenProcessToken(process, TOKEN_DUPLICATE | TOKEN_QUERY, ctypes.byref(source)):
            raise WinError("OpenProcessToken(shell)")
        primary = HANDLE()
        access = (TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE
                  | TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID)
        if not DuplicateTokenEx(source, access, None, SecurityImpersonation, TokenPrimary,
                                ctypes.byref(primary)):
            raise WinError("DuplicateTokenEx(shell)")
        try:
            elevated = _token_elevated(primary)
        except WinError:
            close_handle(primary)
            raise
        return ShellToken(primary, elevated, pid)
    finally:
        close_handle(source)
        close_handle(process)


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", ctypes.c_uint32)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", ctypes.c_uint32), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


def enable_privilege(name):
    """Enable a privilege this process already holds. Returns True when enabled."""
    _require_windows()
    token = HANDLE()
    if not OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                            ctypes.byref(token)):
        raise WinError("OpenProcessToken")
    try:
        luid = LUID()
        if not LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
            raise WinError("LookupPrivilegeValue(%s)" % name)
        privileges = TOKEN_PRIVILEGES()
        privileges.PrivilegeCount = 1
        privileges.Privileges[0].Luid = luid
        privileges.Privileges[0].Attributes = 0x2  # SE_PRIVILEGE_ENABLED
        ctypes.set_last_error(0)
        if not AdjustTokenPrivileges(token, False, ctypes.byref(privileges), 0, None, None):
            raise WinError("AdjustTokenPrivileges(%s)" % name)
        return ctypes.get_last_error() == 0
    finally:
        close_handle(token)


# ===================================================== processes and jobs
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
LOGON_WITH_PROFILE = 0x00000001

JobObjectBasicAccountingInformation = 1
JobObjectBasicProcessIdList = 3
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", DWORD if IS_WINDOWS else ctypes.c_uint32),
                ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
                ("lpTitle", ctypes.c_wchar_p), ("dwX", ctypes.c_uint32), ("dwY", ctypes.c_uint32),
                ("dwXSize", ctypes.c_uint32), ("dwYSize", ctypes.c_uint32),
                ("dwXCountChars", ctypes.c_uint32), ("dwYCountChars", ctypes.c_uint32),
                ("dwFillAttribute", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
                ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
                ("ActiveProcesses", ctypes.c_uint32),
                ("TotalTerminatedProcesses", ctypes.c_uint32)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32), ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JobObject(object):
    """A Windows job that owns one protected application tree."""

    def __init__(self, kill_on_close=False):
        _require_windows()
        self.handle = CreateJobObjectW(None, None)
        if not self.handle:
            raise WinError("CreateJobObject")
        self.kill_on_close = bool(kill_on_close)
        if kill_on_close:
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
                error = WinError("SetInformationJobObject")
                self.close()
                raise error

    def assign(self, process_handle):
        if not AssignProcessToJobObject(self.handle, process_handle):
            raise WinError("AssignProcessToJobObject")

    def active_count(self):
        if not self.handle:
            return 0
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not QueryInformationJobObject(self.handle, JobObjectBasicAccountingInformation,
                                         ctypes.byref(info), ctypes.sizeof(info), None):
            raise WinError("QueryInformationJobObject(accounting)")
        return int(info.ActiveProcesses)

    def pids(self):
        if not self.handle:
            return []
        capacity = 64
        while True:
            size = 8 + ctypes.sizeof(ctypes.c_size_t) * capacity
            buffer = ctypes.create_string_buffer(size)
            ok = QueryInformationJobObject(self.handle, JobObjectBasicProcessIdList, buffer,
                                           size, None)
            if not ok:
                error = ctypes.get_last_error()
                if error == ERROR_MORE_DATA and capacity < 65536:
                    capacity *= 4
                    continue
                raise WinError("QueryInformationJobObject(pid list)", error)
            listed = ctypes.c_uint32.from_buffer(buffer, 4).value
            ids = (ctypes.c_size_t * listed).from_buffer(buffer, 8)
            return [int(v) for v in ids]

    def terminate(self, exit_code=1):
        if self.handle and not TerminateJobObject(self.handle, exit_code):
            raise WinError("TerminateJobObject")

    def close(self):
        if getattr(self, "handle", None):
            close_handle(self.handle)
            self.handle = None


class LaunchedProcess(object):
    __slots__ = ("pid", "process_handle", "job", "de_elevated", "elevated_child")

    def __init__(self, pid, process_handle, job, de_elevated, elevated_child):
        self.pid = pid
        self.process_handle = process_handle
        self.job = job
        self.de_elevated = de_elevated
        self.elevated_child = elevated_child

    def close(self):
        close_handle(self.process_handle)
        self.process_handle = None


def build_command_line(executable, arguments):
    """Windows command line for CreateProcess. Arguments are quoted, never parsed."""
    return subprocess.list2cmdline([executable] + list(arguments))


def create_process_in_job(executable, arguments, working_directory, job,
                          use_shell_token=True, new_console=False):
    """Start *executable* suspended, assign it to *job*, then let it run.

    With *use_shell_token*, an elevated caller starts the child with the
    interactive shell's token -- the user's normal rights -- whenever that
    token is not itself elevated (UAC on). When UAC is off every token in the
    session is already elevated and the child inherits this process's token;
    ``elevated_child`` reports it honestly either way.
    """
    _require_windows()
    command_line = ctypes.create_unicode_buffer(build_command_line(executable, arguments))
    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(STARTUPINFOW)
    info = PROCESS_INFORMATION()
    flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT
    if new_console:
        flags |= CREATE_NEW_CONSOLE
    shell = None
    environment = LPVOID()
    de_elevated = False
    elevated_child = process_is_elevated()
    try:
        if use_shell_token and elevated_child:
            shell = open_shell_token()
            if shell is not None and not shell.elevated:
                if not CreateEnvironmentBlock(ctypes.byref(environment), shell.handle, False):
                    raise WinError("CreateEnvironmentBlock")
                if not CreateProcessWithTokenW(shell.handle, LOGON_WITH_PROFILE, executable,
                                               command_line, flags, environment,
                                               working_directory, ctypes.byref(startup),
                                               ctypes.byref(info)):
                    raise WinError("CreateProcessWithTokenW")
                de_elevated = True
                elevated_child = False
        if not de_elevated:
            if not kernel32.CreateProcessW(executable, command_line, None, None, False,
                                           flags & ~CREATE_UNICODE_ENVIRONMENT, None,
                                           working_directory, ctypes.byref(startup),
                                           ctypes.byref(info)):
                raise WinError("CreateProcess")
    finally:
        if environment:
            DestroyEnvironmentBlock(environment)
        if shell is not None:
            shell.close()
    try:
        job.assign(info.hProcess)
    except WinError:
        TerminateProcess(info.hProcess, 1)
        close_handle(info.hThread)
        close_handle(info.hProcess)
        raise
    if ResumeThread(info.hThread) == 0xFFFFFFFF:
        error = WinError("ResumeThread")
        TerminateProcess(info.hProcess, 1)
        close_handle(info.hThread)
        close_handle(info.hProcess)
        raise error
    close_handle(info.hThread)
    return LaunchedProcess(int(info.dwProcessId), info.hProcess, job, de_elevated,
                           elevated_child)


if IS_WINDOWS:
    kernel32.CreateProcessW.restype = BOOL
    kernel32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, LPVOID, LPVOID, BOOL,
                                        DWORD, LPVOID, wintypes.LPCWSTR, LPVOID, LPVOID]


def process_image_path(pid):
    if not IS_WINDOWS or not pid:
        return None
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        close_handle(handle)


def foreground_pid():
    if not IS_WINDOWS:
        return 0
    hwnd = GetForegroundWindow()
    if not hwnd:
        return 0
    pid = DWORD(0)
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def post_close_to_windows(pids):
    """WM_CLOSE to every visible top-level window owned by *pids*."""
    if not IS_WINDOWS:
        return 0
    wanted = set(int(p) for p in pids)
    if not wanted:
        return 0
    sent = [0]

    def callback(hwnd, _lparam):
        owner = DWORD(0)
        GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in wanted and IsWindowVisible(hwnd):
            PostMessageW(hwnd, 0x0010, 0, 0)
            sent[0] += 1
        return True

    EnumWindows(WNDENUMPROC(callback), 0)
    return sent[0]


def shutdown_block(hwnd, reason):
    if IS_WINDOWS and hwnd:
        return bool(ShutdownBlockReasonCreate(int(hwnd), reason))
    return False


def shutdown_unblock(hwnd):
    if IS_WINDOWS and hwnd:
        return bool(ShutdownBlockReasonDestroy(int(hwnd)))
    return False


# ========================================================= virtual disks
VIRTUAL_STORAGE_TYPE_DEVICE_VHD = 2
VIRTUAL_STORAGE_TYPE_DEVICE_VHDX = 3
VIRTUAL_DISK_ACCESS_NONE = 0
OPEN_VIRTUAL_DISK_FLAG_NONE = 0
OPEN_VIRTUAL_DISK_VERSION_2 = 2
CREATE_VIRTUAL_DISK_VERSION_2 = 2
CREATE_VIRTUAL_DISK_FLAG_NONE = 0
ATTACH_VIRTUAL_DISK_VERSION_1 = 1
ATTACH_VIRTUAL_DISK_FLAG_NO_DRIVE_LETTER = 0x00000002
ATTACH_VIRTUAL_DISK_FLAG_PERMANENT_LIFETIME = 0x00000004
DETACH_VIRTUAL_DISK_FLAG_NONE = 0
GET_VIRTUAL_DISK_INFO_IS_LOADED = 13


class VIRTUAL_STORAGE_TYPE(ctypes.Structure):
    _fields_ = [("DeviceId", ctypes.c_uint32), ("VendorId", GUID if IS_WINDOWS else ctypes.c_byte * 16)]


class OPEN_VIRTUAL_DISK_PARAMETERS(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_int), ("GetInfoOnly", ctypes.c_int),
                ("ReadOnly", ctypes.c_int), ("ResiliencyGuid", ctypes.c_byte * 16),
                ("SnapshotId", ctypes.c_byte * 16)]


class CREATE_VIRTUAL_DISK_PARAMETERS_V2(ctypes.Structure):
    _fields_ = [("UniqueId", ctypes.c_byte * 16), ("MaximumSize", ctypes.c_uint64),
                ("BlockSizeInBytes", ctypes.c_uint32), ("SectorSizeInBytes", ctypes.c_uint32),
                ("PhysicalSectorSizeInBytes", ctypes.c_uint32),
                ("ParentPath", ctypes.c_wchar_p), ("SourcePath", ctypes.c_wchar_p),
                ("OpenFlags", ctypes.c_uint32),
                ("ParentVirtualStorageType", VIRTUAL_STORAGE_TYPE),
                ("SourceVirtualStorageType", VIRTUAL_STORAGE_TYPE),
                ("ResiliencyGuid", ctypes.c_byte * 16)]


class CREATE_VIRTUAL_DISK_PARAMETERS(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_int), ("Version2", CREATE_VIRTUAL_DISK_PARAMETERS_V2),
                ("_reserve", ctypes.c_byte * 64)]


class ATTACH_VIRTUAL_DISK_PARAMETERS(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_int), ("_pad", ctypes.c_int),
                ("Reserved1", ctypes.c_uint64), ("Reserved2", ctypes.c_uint64)]


def _vhdx_storage_type(path):
    storage = VIRTUAL_STORAGE_TYPE()
    extension = os.path.splitext(path)[1].lower()
    storage.DeviceId = VIRTUAL_STORAGE_TYPE_DEVICE_VHD if extension == ".vhd" else \
        VIRTUAL_STORAGE_TYPE_DEVICE_VHDX
    vendor = storage.VendorId
    vendor.Data1 = 0xEC984AEC
    vendor.Data2 = 0xA0F9
    vendor.Data3 = 0x47E9
    for index, value in enumerate((0x90, 0x1F, 0x71, 0x41, 0x5A, 0x66, 0x34, 0x5B)):
        vendor.Data4[index] = value
    return storage


def _check_virtdisk():
    _require_windows()
    if virtdisk is None:
        raise WinError("virtdisk.dll", 126)


def create_vhdx(path, size_bytes):
    """Create a dynamically expanding VHDX. The file must not exist."""
    _check_virtdisk()
    if os.path.exists(path):
        raise WinError("CreateVirtualDisk(exists)", 80)
    storage = _vhdx_storage_type(path)
    params = CREATE_VIRTUAL_DISK_PARAMETERS()
    params.Version = CREATE_VIRTUAL_DISK_VERSION_2
    params.Version2.MaximumSize = int(size_bytes) // 512 * 512
    handle = HANDLE()
    result = CreateVirtualDisk(ctypes.byref(storage), path, VIRTUAL_DISK_ACCESS_NONE, None,
                               CREATE_VIRTUAL_DISK_FLAG_NONE, 0, ctypes.byref(params), None,
                               ctypes.byref(handle))
    if result != 0:
        raise WinError("CreateVirtualDisk", result)
    close_handle(handle)


class VirtualDisk(object):
    """An open handle on a VHD(X) file."""

    def __init__(self, path, info_only=False):
        _check_virtdisk()
        self.path = path
        storage = _vhdx_storage_type(path)
        params = OPEN_VIRTUAL_DISK_PARAMETERS()
        params.Version = OPEN_VIRTUAL_DISK_VERSION_2
        params.GetInfoOnly = 1 if info_only else 0
        params.ReadOnly = 0
        handle = HANDLE()
        result = OpenVirtualDisk(ctypes.byref(storage), path, VIRTUAL_DISK_ACCESS_NONE,
                                 OPEN_VIRTUAL_DISK_FLAG_NONE, ctypes.byref(params),
                                 ctypes.byref(handle))
        if result != 0:
            raise WinError("OpenVirtualDisk", result)
        self.handle = handle
        self.attached_here = False

    def is_loaded(self):
        buffer = ctypes.create_string_buffer(64)
        ctypes.c_int.from_buffer(buffer, 0).value = GET_VIRTUAL_DISK_INFO_IS_LOADED
        size = wintypes.ULONG(64)
        result = GetVirtualDiskInformation(self.handle, ctypes.byref(size), buffer, None)
        if result != 0:
            raise WinError("GetVirtualDiskInformation(IS_LOADED)", result)
        return bool(ctypes.c_int.from_buffer(buffer, 8).value)

    def physical_path(self):
        size = wintypes.ULONG(520)
        buffer = ctypes.create_unicode_buffer(260)
        result = GetVirtualDiskPhysicalPath(self.handle, ctypes.byref(size), buffer)
        if result != 0:
            raise WinError("GetVirtualDiskPhysicalPath", result)
        return buffer.value

    def disk_number(self):
        path = self.physical_path()
        digits = "".join(ch for ch in path[-6:] if ch.isdigit())
        tail = path.rsplit("PhysicalDrive", 1)
        if len(tail) == 2 and tail[1].isdigit():
            return int(tail[1])
        if digits:
            return int(digits)
        raise WinError("physical path parse", 1)

    def attach(self, permanent=False):
        enable_privilege("SeManageVolumePrivilege")
        params = ATTACH_VIRTUAL_DISK_PARAMETERS()
        params.Version = ATTACH_VIRTUAL_DISK_VERSION_1
        flags = ATTACH_VIRTUAL_DISK_FLAG_NO_DRIVE_LETTER
        if permanent:
            flags |= ATTACH_VIRTUAL_DISK_FLAG_PERMANENT_LIFETIME
        result = AttachVirtualDisk(self.handle, None, flags, 0, ctypes.byref(params), None)
        if result != 0:
            raise WinError("AttachVirtualDisk", result)
        self.attached_here = True

    def detach(self):
        result = DetachVirtualDisk(self.handle, DETACH_VIRTUAL_DISK_FLAG_NONE, 0)
        if result != 0:
            raise WinError("DetachVirtualDisk", result)
        self.attached_here = False

    def close(self):
        if getattr(self, "handle", None):
            close_handle(self.handle)
            self.handle = None
            self.attached_here = False


# =============================================================== volumes
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080
FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_ARCHIVE = 0x20
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_NOT_CONTENT_INDEXED = 0x2000
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def with_trailing_backslash(path):
    return path if path.endswith("\\") else path + "\\"


def volume_device_path(volume_name):
    """``\\\\?\\Volume{GUID}\\`` -> ``\\\\.\\Volume{GUID}`` for CreateFile."""
    name = volume_name.rstrip("\\")
    if name.startswith("\\\\?\\"):
        name = "\\\\.\\" + name[4:]
    return name


def list_volumes():
    _require_windows()
    buffer = ctypes.create_unicode_buffer(1024)
    handle = FindFirstVolumeW(buffer, len(buffer))
    if handle in (None, INVALID_HANDLE_VALUE):
        raise WinError("FindFirstVolume")
    names = [buffer.value]
    try:
        while FindNextVolumeW(handle, buffer, len(buffer)):
            names.append(buffer.value)
        error = ctypes.get_last_error()
        if error not in (ERROR_NO_MORE_FILES, 0):
            raise WinError("FindNextVolume", error)
    finally:
        FindVolumeClose(handle)
    return names


def _open_device(path, access):
    handle = CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING,
                         0, None)
    if handle in (None, INVALID_HANDLE_VALUE):
        raise WinError("CreateFile(%s)" % path)
    return handle


def volume_disk_numbers(volume_name):
    """Disk numbers a volume's extents live on. Empty for a volume with none."""
    handle = _open_device(volume_device_path(volume_name), 0)
    try:
        size = 8 + 24 * 16
        buffer = ctypes.create_string_buffer(size)
        returned = DWORD(0)
        if not DeviceIoControl(handle, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, None, 0, buffer,
                               size, ctypes.byref(returned), None):
            error = ctypes.get_last_error()
            if error in (1, 50, 87, 21):
                return []
            raise WinError("IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS", error)
        count = ctypes.c_uint32.from_buffer(buffer, 0).value
        return [ctypes.c_uint32.from_buffer(buffer, 8 + 24 * i).value for i in range(min(count, 16))]
    finally:
        close_handle(handle)


def volumes_on_disk(disk_number):
    out = []
    for name in list_volumes():
        try:
            if int(disk_number) in volume_disk_numbers(name):
                out.append(name)
        except WinError:
            continue
    return out


def volume_mount_points(volume_name):
    """Every access path (drive letters and folder mount points) of a volume."""
    _require_windows()
    size = DWORD(1024)
    while True:
        buffer = ctypes.create_unicode_buffer(size.value)
        needed = DWORD(0)
        if GetVolumePathNamesForVolumeNameW(volume_name, buffer, size.value, ctypes.byref(needed)):
            break
        error = ctypes.get_last_error()
        if error == ERROR_MORE_DATA and needed.value > size.value:
            size = DWORD(needed.value)
            continue
        if error in (ERROR_FILE_NOT_FOUND, ERROR_NOT_READY):
            return []
        raise WinError("GetVolumePathNamesForVolumeName", error)
    raw = ctypes.wstring_at(ctypes.addressof(buffer), size.value)
    return [part for part in raw.split("\x00") if part]


def volume_for_mount_point(path):
    """The volume mounted at *path*, or None when *path* is not a mount point."""
    _require_windows()
    buffer = ctypes.create_unicode_buffer(128)
    if GetVolumeNameForVolumeMountPointW(with_trailing_backslash(path), buffer, len(buffer)):
        return buffer.value
    return None


def set_mount_point(path, volume_name):
    if not SetVolumeMountPointW(with_trailing_backslash(path), with_trailing_backslash(volume_name)):
        raise WinError("SetVolumeMountPoint")


def delete_mount_point(path):
    if not DeleteVolumeMountPointW(with_trailing_backslash(path)):
        raise WinError("DeleteVolumeMountPoint")


class VolumeLock(object):
    """Exclusive lock on a mounted volume.

    ``FSCTL_LOCK_VOLUME`` succeeds only when no other handle is open on the
    volume. That makes it the broker's proof that nothing -- the protected
    application, an Explorer window, an indexer -- still has a file open
    before storage is detached. :meth:`acquire` returns False instead of
    raising when the volume is busy.
    """

    def __init__(self, volume_name):
        self.volume_name = volume_name
        self.handle = None
        self.locked = False

    def acquire(self, flush=True):
        self.handle = _open_device(volume_device_path(self.volume_name),
                                   GENERIC_READ | GENERIC_WRITE)
        if flush:
            FlushFileBuffers(self.handle)
        returned = DWORD(0)
        if not DeviceIoControl(self.handle, FSCTL_LOCK_VOLUME, None, 0, None, 0,
                               ctypes.byref(returned), None):
            error = ctypes.get_last_error()
            self.release()
            if error in (ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
                return False
            raise WinError("FSCTL_LOCK_VOLUME", error)
        self.locked = True
        return True

    def dismount(self):
        returned = DWORD(0)
        if not DeviceIoControl(self.handle, FSCTL_DISMOUNT_VOLUME, None, 0, None, 0,
                               ctypes.byref(returned), None):
            raise WinError("FSCTL_DISMOUNT_VOLUME")

    def release(self):
        if self.handle:
            if self.locked:
                returned = DWORD(0)
                DeviceIoControl(self.handle, FSCTL_UNLOCK_VOLUME, None, 0, None, 0,
                                ctypes.byref(returned), None)
                self.locked = False
            close_handle(self.handle)
            self.handle = None


def file_attributes(path):
    if not IS_WINDOWS:
        return 0
    value = GetFileAttributesW(path)
    return None if value == INVALID_FILE_ATTRIBUTES else int(value)


def copy_attribute_bits(source, destination):
    """Carry hidden/system/readonly/archive/not-indexed bits across a copy."""
    attrs = file_attributes(source)
    if attrs is None:
        return False
    mask = (FILE_ATTRIBUTE_READONLY | FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
            | FILE_ATTRIBUTE_ARCHIVE | FILE_ATTRIBUTE_NOT_CONTENT_INDEXED)
    current = file_attributes(destination)
    if current is None:
        return False
    wanted = (current & ~mask) | (attrs & mask)
    if wanted == current:
        return True
    return bool(SetFileAttributesW(destination, wanted))
