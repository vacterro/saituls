"""Protected-application lifecycle for SAITULS Secure Apps.

Two jobs, both of which the secure-lock sequence depends on being exact:

1. **Own the process tree.** Obsidian is Electron: the launched process spawns
   renderer and GPU children, and the parent can exit while children still
   hold handles on the vault. "Did the application exit?" therefore means
   "did every process in the tree exit?", never "did the pid I spawned go
   away?". Detaching writable storage while a child still owns an open handle
   is the one failure mode that loses data, so the unmount step never runs
   until this module confirms the whole tree is gone.

2. **Define protected activity.** The six-hour idle timer is about the
   protected application, not about the machine. Mouse movement in a browser
   is not activity here. What counts is: a protected launch, the protected
   application owning the foreground window, or an explicit Secure Apps
   interaction. :meth:`AppSupervisor.foreground_is_protected` implements the
   middle one by resolving the foreground window's owning pid and asking
   whether it belongs to the tracked tree.

The Windows specifics sit behind :class:`ProcessAdapter` so the lifecycle
tests can run the full state machine against :class:`FakeProcessAdapter`
without launching anything.
"""
import os
import subprocess
import time


class AppLaunchError(Exception):
    category = "app_launch_failed"


class AppExitTimeout(Exception):
    category = "app_exit_timeout"


class ProcessAdapter(object):
    """Real Windows implementation. psutil for the tree, user32 for the close."""

    def spawn(self, executable, arguments, working_directory):
        if not os.path.isfile(executable):
            raise AppLaunchError("protected application not found: %s" % executable)
        if working_directory and not os.path.isdir(working_directory):
            raise AppLaunchError("working directory not found: %s" % working_directory)
        argv = [executable] + list(arguments)
        try:
            # No shell, ever: the registry stores an executable and an argument
            # list precisely so that nothing here has to parse a command string.
            creationflags = 0
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
            proc = subprocess.Popen(argv, cwd=working_directory or None,
                                    shell=False, close_fds=True,
                                    creationflags=creationflags)
        except OSError as exc:
            raise AppLaunchError("could not start the protected application (%s)"
                                 % type(exc).__name__)
        return proc.pid

    def alive(self, pid):
        if not pid:
            return False
        try:
            import psutil
        except ImportError:
            return False
        try:
            proc = psutil.Process(int(pid))
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    def tree_pids(self, pid):
        if not pid:
            return []
        try:
            import psutil
        except ImportError:
            return [int(pid)] if self.alive(pid) else []
        try:
            root = psutil.Process(int(pid))
        except Exception:
            return []
        pids = [root.pid]
        try:
            for child in root.children(recursive=True):
                pids.append(child.pid)
        except Exception:
            pass
        return pids

    def request_close(self, pid):
        """Post WM_CLOSE to every visible top-level window the tree owns."""
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return 0
        pids = set(self.tree_pids(pid))
        if not pids:
            return 0
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WM_CLOSE = 0x0010
        sent = [0]
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value in pids and user32.IsWindowVisible(hwnd):
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                sent[0] += 1
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(callback), 0)
        except Exception:
            return sent[0]
        return sent[0]

    def terminate_tree(self, pid):
        try:
            import psutil
        except ImportError:
            return
        try:
            root = psutil.Process(int(pid))
        except Exception:
            return
        victims = []
        try:
            victims = root.children(recursive=True)
        except Exception:
            pass
        victims.append(root)
        for proc in victims:
            try:
                proc.terminate()
            except Exception:
                pass
        gone, alive = psutil.wait_procs(victims, timeout=5)
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass

    def foreground_pid(self):
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return 0
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return 0
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            return int(owner.value)
        except Exception:
            return 0


class FakeProcessAdapter(ProcessAdapter):
    """Deterministic adapter for lifecycle tests. Spawns nothing."""

    def __init__(self):
        self.next_pid = 4100
        self.alive_pids = set()
        self.trees = {}
        self.spawned = []
        self.close_requests = []
        self.terminations = []
        self.fail_spawn = None
        self._foreground = 0
        self.ignore_close = False

    def spawn(self, executable, arguments, working_directory):
        self.spawned.append((executable, list(arguments), working_directory))
        if self.fail_spawn:
            raise AppLaunchError(self.fail_spawn)
        pid = self.next_pid
        self.next_pid += 1
        self.alive_pids.add(pid)
        self.trees[pid] = [pid, pid + 1000]
        self.alive_pids.add(pid + 1000)
        return pid

    def alive(self, pid):
        return int(pid) in self.alive_pids

    def tree_pids(self, pid):
        return [p for p in self.trees.get(int(pid), [int(pid)]) if p in self.alive_pids]

    def request_close(self, pid):
        self.close_requests.append(int(pid))
        if self.ignore_close:
            return 1
        self.exit_tree(pid)
        return 1

    def terminate_tree(self, pid):
        self.terminations.append(int(pid))
        self.exit_tree(pid)

    def exit_tree(self, pid):
        for p in list(self.trees.get(int(pid), [int(pid)])):
            self.alive_pids.discard(p)

    def exit_parent_only(self, pid):
        """A child outlives the parent -- the Electron case the design fears."""
        self.alive_pids.discard(int(pid))

    def set_foreground(self, pid):
        self._foreground = int(pid or 0)

    def foreground_pid(self):
        return self._foreground


class ProcessObservation(object):
    """Ephemeral per‑turn snapshot: one tree + one foreground lookup."""
    __slots__ = ("root_pid", "tree_pids", "foreground_pid", "running", "foreground_protected")

    def __init__(self, root_pid, tree_pids, foreground_pid, running, foreground_protected):
        self.root_pid = root_pid
        self.tree_pids = tree_pids
        self.foreground_pid = foreground_pid
        self.running = running
        self.foreground_protected = foreground_protected


class AppSupervisor(object):
    """Tracks one protected application instance for one profile."""

    def __init__(self, adapter=None, clock=time.monotonic, sleep=time.sleep):
        self.adapter = adapter or ProcessAdapter()
        self.clock = clock
        self.sleep = sleep
        self.pid = 0
        self.launched_at = None

    # -- launch -----------------------------------------------------------
    def launch(self, executable, arguments, working_directory):
        pid = self.adapter.spawn(executable, arguments, working_directory)
        self.pid = int(pid)
        self.launched_at = self.clock()
        return self.pid

    def attach(self, pid):
        self.pid = int(pid or 0)

    def forget(self):
        self.pid = 0
        self.launched_at = None

    # -- observation ------------------------------------------------------
    @property
    def running(self):
        return bool(self.pid) and bool(self.adapter.tree_pids(self.pid))

    def tree(self):
        return self.adapter.tree_pids(self.pid) if self.pid else []

    def observation(self):
        """One snapshot per profile per periodic turn (SRC-027 PERF-004).

        Captures the tree once and optionally the foreground pid; subsequent
        derives reuse this instead of re‑enumerating.
        """
        if not self.pid:
            return ProcessObservation(self.pid, [], None, False, False)
        tree = self.adapter.tree_pids(self.pid) or []
        running = bool(tree)
        fg = self.adapter.foreground_pid()
        protected = bool(fg) and int(fg) in set(tree) if running else False
        return ProcessObservation(self.pid, tree, fg, running, protected)

    def running_from(self, obs):
        return bool(obs.running) if isinstance(obs, ProcessObservation) else self.running

    def foreground_is_protected_from(self, obs):
        """Same as foreground_is_protected() but reuses a prior snapshot."""
        if not self.pid:
            return False
        if isinstance(obs, ProcessObservation):
            return bool(obs.foreground_protected)
        return self.foreground_is_protected()

    def foreground_is_protected(self):
        """True only when the protected tree owns the foreground window."""
        if not self.pid:
            return False
        fg = self.adapter.foreground_pid()
        if not fg:
            return False
        return int(fg) in set(self.adapter.tree_pids(self.pid))

    # -- shutdown ---------------------------------------------------------
    def request_graceful_close(self):
        if not self.pid:
            return 0
        return self.adapter.request_close(self.pid)

    def wait_for_exit(self, timeout, poll_interval=0.25):
        """Wait for the whole tree. Returns True when every pid is gone."""
        if not self.pid:
            return True
        deadline = self.clock() + max(0.0, float(timeout))
        while True:
            if not self.adapter.tree_pids(self.pid):
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(poll_interval)

    def terminate(self):
        if self.pid:
            self.adapter.terminate_tree(self.pid)

    def close_sequence(self, graceful_timeout, force_terminate,
                       confirm_timeout, poll_interval=0.25):
        """Graceful close, bounded wait, optional force, confirmed exit.

        Returns ``(exited, forced)``. ``exited`` False means a process in the
        tree is still alive: the caller must NOT detach storage.
        """
        if not self.running:
            self.forget()
            return True, False
        self.request_graceful_close()
        if self.wait_for_exit(graceful_timeout, poll_interval):
            self.forget()
            return True, False
        if not force_terminate:
            return False, False
        self.terminate()
        exited = self.wait_for_exit(confirm_timeout, poll_interval)
        if exited:
            self.forget()
        return exited, True
