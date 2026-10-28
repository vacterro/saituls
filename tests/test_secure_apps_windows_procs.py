"""Real Windows process-lifetime tests for the protected application.

The hermetic suite (tests/test_secure_apps.py) drives
:class:`~sa_applife.FakeProcessAdapter`, which models Electron's shape
correctly. It cannot, however, prove that the REAL adapter survives a parent
exit -- a fake that behaves better than the code it stands in for is not
evidence about that code, and the fake once did exactly that: the real
``tree_pids`` returned ``[]`` the instant the launched process died, which is
what made a live renderer look like a stopped application and could unmount
a vault out from under it.

So this file spawns real processes and asks the real adapter:

  * a parent that starts a child, reports the child's pid, holds long enough
    to be observed, and then exits;
  * a child that stays alive for a bounded interval afterwards.

The supervisor must keep reporting RUNNING after the parent is gone, and only
report stopped once the child itself exits. The reverse case is pinned too: a
supervisor handed a pid that is ALREADY dead owns nothing and claims nothing,
because nothing links an unseen process to the launch it did not witness.

Nothing here touches a vault, a VHDX, a security key or the network. Every
process is a short-lived copy of this interpreter.
"""
import os
import subprocess
import sys
import time
import unittest
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_applife  # noqa: E402

CHILD_SOURCE = (
    "import sys, time\n"
    "time.sleep(float(sys.argv[1]))\n"
)

#: argv[1] = child lifetime, argv[2] = how long the parent stays alive after
#: reporting the child pid. It must outlive the supervisor's first
#: observation, otherwise the test would be asserting that a process the
#: supervisor never saw can still be adopted -- which is the opposite claim.
PARENT_SOURCE = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', CHILD_SOURCE, sys.argv[1]])\n"
    "sys.stdout.write('%d\\n' % child.pid)\n"
    "sys.stdout.flush()\n"
    "time.sleep(float(sys.argv[2]))\n"
).replace("CHILD_SOURCE", repr(CHILD_SOURCE))

CHILD_SECONDS = 20.0
PARENT_HOLD_SECONDS = 2.0

# One off-screen visible HWND, TTL 10s; no console, vault or operator window.
WINDOW_SOURCE = r'''
import ctypes, sys
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32',use_last_error=True)
u=ctypes.WinDLL('user32',use_last_error=True)
def fn(d,n,r,*a):
    f=getattr(d,n);f.restype=r;f.argtypes=list(a);return f
Proc=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM)
class WC(ctypes.Structure):
    _fields_=[('style',w.UINT),('proc',Proc),('class_extra',ctypes.c_int),('window_extra',ctypes.c_int),('instance',w.HINSTANCE),('icon',w.HICON),('cursor',w.HANDLE),('background',w.HBRUSH),('menu',w.LPCWSTR),('name',w.LPCWSTR)]
default=fn(u,'DefWindowProcW',ctypes.c_ssize_t,w.HWND,w.UINT,w.WPARAM,w.LPARAM)
destroy=fn(u,'DestroyWindow',w.BOOL,w.HWND)
quit=fn(u,'PostQuitMessage',None,ctypes.c_int)
def handler(hwnd,msg,wp,lp):
    if msg==0x10:
        with open(sys.argv[1],'w') as f:f.write('WM_CLOSE\n')
        destroy(hwnd);return 0
    if msg==0x113:destroy(hwnd);return 0
    if msg==2:quit(0);return 0
    return default(hwnd,msg,wp,lp)
callback=Proc(handler)
module=fn(k,'GetModuleHandleW',w.HINSTANCE,w.LPCWSTR)(None)
wc=WC();wc.proc=callback;wc.instance=module;wc.name='SAITULS_T247_Disposable'
if not fn(u,'RegisterClassW',w.WORD,ctypes.POINTER(WC))(ctypes.byref(wc)):raise ctypes.WinError(ctypes.get_last_error())
hwnd=fn(u,'CreateWindowExW',w.HWND,w.DWORD,w.LPCWSTR,w.LPCWSTR,w.DWORD,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,w.HWND,w.HMENU,w.HINSTANCE,ctypes.c_void_p)(0,wc.name,'SAITULS T247 disposable oracle',0x90000000,-32000,-32000,16,16,None,None,module,None)
if not hwnd:raise ctypes.WinError(ctypes.get_last_error())
fn(u,'SetTimer',ctypes.c_size_t,w.HWND,ctypes.c_size_t,w.UINT,ctypes.c_void_p)(hwnd,1,10000,None)
print(int(hwnd),flush=True)
msg=w.MSG()
get=fn(u,'GetMessageW',w.BOOL,ctypes.POINTER(w.MSG),w.HWND,w.UINT,w.UINT)
translate=fn(u,'TranslateMessage',w.BOOL,ctypes.POINTER(w.MSG))
dispatch=fn(u,'DispatchMessageW',ctypes.c_ssize_t,ctypes.POINTER(w.MSG))
while get(ctypes.byref(msg),None,0,0)>0:translate(ctypes.byref(msg));dispatch(ctypes.byref(msg))
'''


def _psutil():
    try:
        import psutil
    except ImportError:
        return None
    return psutil


PSUTIL = _psutil()
WINDOWS = os.name == "nt"

requires_real_windows = unittest.skipUnless(
    WINDOWS and PSUTIL is not None,
    "real Windows process lifetime: needs Windows and psutil")


def _wait_gone(pid, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if PSUTIL.pid_exists(int(pid)):
            time.sleep(0.05)
            continue
        return True
    return not PSUTIL.pid_exists(int(pid))


@requires_real_windows
class RealAdapterChildSurvivalTests(unittest.TestCase):
    """The Electron case, against real processes and the real adapter."""

    def _start(self):
        parent = subprocess.Popen(
            [sys.executable, "-c", PARENT_SOURCE,
             repr(CHILD_SECONDS), repr(PARENT_HOLD_SECONDS)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL)
        line = parent.stdout.readline()
        self.assertTrue(line, "the parent never reported a child pid")
        child_pid = int(line.strip())
        self.assertTrue(PSUTIL.pid_exists(child_pid), "child never started")
        self.addCleanup(_wait_gone, child_pid)
        return parent, child_pid

    def test_surviving_child_keeps_the_application_running(self):
        parent, child_pid = self._start()
        adapter = sa_applife.ProcessAdapter()
        supervisor = sa_applife.AppSupervisor(adapter=adapter)

        # Observed while the root still lives, exactly like a real launch.
        supervisor.attach(parent.pid)
        family = supervisor.family
        self.assertIn(parent.pid, family)
        self.assertIn(child_pid, family)
        self.assertTrue(all(isinstance(v, float) for v in family.values()),
                        "family must carry creation times, not bare pids")

        parent.wait(timeout=30)
        self.assertFalse(PSUTIL.pid_exists(parent.pid))

        # The defect this file exists for: the root is gone, so a root-anchored
        # walk finds nothing at all -- yet the renderer is still alive.
        self.assertEqual(adapter.tree_pids(parent.pid), [])
        self.assertTrue(PSUTIL.pid_exists(child_pid))

        self.assertTrue(supervisor.running,
                        "an orphaned live child must still count as running")
        self.assertIn(child_pid, supervisor.surviving_family())

        obs = supervisor.observation()
        self.assertTrue(obs.running)
        self.assertIn(child_pid, obs.tree_pids)

        deadline = time.time() + CHILD_SECONDS + 15.0
        while time.time() < deadline and supervisor.running:
            time.sleep(0.2)
        self.assertFalse(supervisor.running,
                         "stopped only after the surviving child actually exited")

    def test_orphan_child_is_terminated_as_family(self):
        """close_sequence's force step must reach a process whose parent died."""
        parent, child_pid = self._start()
        adapter = sa_applife.ProcessAdapter()
        supervisor = sa_applife.AppSupervisor(adapter=adapter)
        supervisor.attach(parent.pid)
        parent.wait(timeout=30)
        self.assertTrue(supervisor.running)
        supervisor.terminate()
        self.assertTrue(_wait_gone(child_pid, 20.0),
                        "the orphan was not terminated: it is still the only "
                        "process left holding the vault open")
        self.assertFalse(supervisor.running)

    def test_a_dead_root_is_never_adopted(self):
        """No observation, no ownership. Fail closed rather than guess."""
        parent, child_pid = self._start()
        parent.wait(timeout=30)
        self.assertTrue(PSUTIL.pid_exists(child_pid))
        adapter = sa_applife.ProcessAdapter()
        supervisor = sa_applife.AppSupervisor(adapter=adapter)
        supervisor.attach(parent.pid)
        self.assertFalse(supervisor.running)
        self.assertNotIn(child_pid, supervisor.surviving_family())
        self.assertEqual(supervisor.family, {})

    def test_identity_is_creation_time_not_bare_pid(self):
        """A pid that exists is not proof it is the process we launched."""
        adapter = sa_applife.ProcessAdapter()
        me = os.getpid()
        identity = adapter.identity(me)
        self.assertIsNotNone(identity)
        pid, created = identity
        self.assertEqual(pid, me)
        self.assertTrue(adapter.identity_matches(me, created))
        # A recycled pid is born later; that difference is the whole defence.
        self.assertFalse(adapter.identity_matches(me, created + 3600.0))
        self.assertFalse(adapter.identity_matches(me, None))
        self.assertIsNone(adapter.identity(0))
        self.assertIsNone(adapter.identity(0x7FFFFFF0))

    def test_wrong_birth_never_terminates_real_disposable_process(self):
        child = subprocess.Popen([sys.executable, "-c", CHILD_SOURCE, "10"],
                                 stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self._stop_owned, child)
        adapter = sa_applife.ProcessAdapter()
        identity = adapter.identity(child.pid)
        self.assertIsNotNone(identity)
        adapter.terminate_pids([(child.pid, identity[1] + 3600)])
        self.assertIsNone(child.poll(), "wrong birth targeted the real process")
        self.assertTrue(adapter.identity_matches(*identity))
        adapter.terminate_pids([identity])
        child.wait(timeout=10)
        self.assertFalse(adapter.identity_matches(*identity))

    def test_native_handle_birth_and_lifetime_are_bound(self):
        child = subprocess.Popen([sys.executable, "-c", CHILD_SOURCE, "10"],
                                 stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self._stop_owned, child)
        adapter = sa_applife.ProcessAdapter()
        identity = adapter.identity(child.pid)
        pin = adapter._pin_process(*identity, destructive=True)
        self.assertIsNotNone(pin)
        try:
            self.assertTrue(pin.matches(identity[1]))
            self.assertFalse(pin.matches(identity[1] + 3600))
            self.assertIsNone(adapter._pin_process(child.pid, identity[1] + 3600, destructive=True))
            pin.terminate()
            child.wait(timeout=10)
            self.assertFalse(pin.matches(identity[1]), "dead process handle was still live")
        finally:
            pin.close()
        self.assertIsNone(pin.handle)

    def test_real_window_close_requires_correct_birth(self):
        with tempfile.TemporaryDirectory(prefix="saituls-T247-window-") as directory:
            marker = Path(directory) / "closed.txt"
            child = subprocess.Popen([sys.executable, "-c", WINDOW_SOURCE, str(marker)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                line = child.stdout.readline()
                self.assertTrue(line, child.stderr.read().decode("utf-8", "replace") if child.poll() is not None else "no HWND")
                self.assertGreater(int(line.strip()), 0)
                adapter = sa_applife.ProcessAdapter()
                identity = adapter.identity(child.pid)
                self.assertEqual(adapter.request_close(child.pid, [(child.pid, identity[1] + 3600)]), 0)
                self.assertIsNone(child.poll())
                self.assertFalse(marker.exists())
                self.assertEqual(adapter.request_close(child.pid, [identity]), 1)
                child.wait(timeout=10)
                self.assertEqual(marker.read_text().strip(), "WM_CLOSE")
            finally:
                self._stop_owned(child)
                child.stdout.close()
                child.stderr.close()

    @staticmethod
    def _stop_owned(child):
        # Popen retains the original Windows process handle; never resolve a
        # replacement by numeric PID during test cleanup.
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
