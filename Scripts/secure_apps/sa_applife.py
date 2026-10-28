"""Protected-application lifecycle for SAITULS Secure Apps.

Two jobs, both of which the secure-lock sequence depends on being exact:

1. **Own the process tree.** Obsidian is Electron: the launched process spawns
   renderer and GPU children, and the parent can exit while children still
   hold handles on the vault. "Did the application exit?" therefore means
   "did every process in the tree exit?", never "did the pid I spawned go
   away?". Detaching writable storage while a child still owns an open handle
   is the one failure mode that loses data, so the unmount step never runs
   until this module confirms the whole tree is gone.

   The parent pid is therefore NOT the identity of the protected application.
   It is only where discovery starts. The identity is the durable set of
   ``(pid, creation_time)`` pairs the supervisor has actually seen, and a
   member survives the death of its parent because every member is
   revalidated on its own. A bare pid is never continuity: Windows reuses
   pids, and treating a recycled pid as "the protected application still
   running" would either hold the vault open forever or -- worse -- let an
   unrelated process inherit protected status.

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

Process identity
---------------
Every liveness question is asked as "is this the same process I saw before?",
never "does this pid exist?". :func:`ProcessAdapter.identity` answers with a
``(pid, creation_time)`` pair or ``None``; :func:`identity_matches` is the
comparison the supervisor prunes on, so a recycled pid can never inherit
protected status. ``tests/test_secure_apps_windows_procs.py`` exercises the
real adapter against real Windows parent/child lifetime.
"""
import os
import math
import subprocess
import time

#: Secondary tolerance between native FILETIME conversion and psutil's epoch
#: representation. Canonical adapter-to-adapter birth readings match EXACTLY;
#: this tolerance never admits a replacement PID at the destructive boundary.
CREATE_TIME_EPSILON = 0.01

_PSUTIL = None


def psutil_or_none():
    """psutil, imported once. None when it is not installed."""
    global _PSUTIL
    if _PSUTIL is None:
        try:
            import psutil
        except ImportError:
            _PSUTIL = False
        else:
            _PSUTIL = psutil
    return _PSUTIL or None


class AppLaunchError(Exception):
    category = "app_launch_failed"

    def __init__(self, message, category=None):
        Exception.__init__(self, message)
        if category:
            self.category = category


class AppExitTimeout(Exception):
    category = "app_exit_timeout"


class _IdentityHandle(object):
    """Hold one Windows process object throughout a bounded destructive operation."""

    def __init__(self, pid, destructive=False):
        import sa_win
        self.win = sa_win
        access = sa_win.PROCESS_QUERY_LIMITED_INFORMATION | sa_win.SYNCHRONIZE
        if destructive:
            access |= sa_win.PROCESS_TERMINATE
        self.handle = sa_win.OpenProcess(access, False, int(pid))
        if not self.handle:
            raise sa_win.WinError("OpenProcess(identity)")

    def matches(self, created):
        return (self.win.WaitForSingleObject(self.handle, 0) == 258 and
                abs(self.win.process_creation_time(self.handle) - float(created)) <= CREATE_TIME_EPSILON)

    def terminate(self):
        if not self.win.TerminateProcess(self.handle, 1):
            raise self.win.WinError("TerminateProcess(identity)")

    def kill(self):
        self.terminate()

    def close(self):
        if self.handle:
            self.win.close_handle(self.handle)
            self.handle = None


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
        psutil = psutil_or_none()
        if psutil is None:
            return False
        try:
            proc = psutil.Process(int(pid))
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    def identity(self, pid):
        """``(pid, creation_time)`` for a live process, else ``None``.

        This is the only honest way to ask "is the process I launched still
        there?". ``psutil.Process(pid)`` succeeding proves a pid is in use,
        nothing more; the creation time is what ties that pid to the process
        this supervisor started.
        """
        if not pid:
            return None
        psutil = psutil_or_none()
        if psutil is None:
            raise AppExitTimeout("process identity support is unavailable")
        try:
            proc = psutil.Process(int(pid))
            if proc.status() == psutil.STATUS_ZOMBIE:
                return None
            return (int(proc.pid), float(proc.create_time()))
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None
        except Exception as exc:
            # Unreadable is not confirmed exit. Keep the existing family and
            # let the broker fail the operation without detaching storage.
            raise AppExitTimeout("could not verify protected process identity") from exc

    def identity_matches(self, pid, created):
        """True only when *pid* is still the process born at *created*."""
        identity = self.identity(pid)
        if identity is None:
            return False
        try:
            # Both values came through this same adapter/clock conversion.
            # Even a replacement born within the old tolerance is foreign.
            return math.isfinite(float(created)) and identity[1] == float(created)
        except (TypeError, ValueError):
            return False

    def tree_identities(self, pid):
        """``(pid, creation_time)`` for *pid* and every live descendant.

        Empty when *pid* itself is gone: Windows cannot enumerate children of
        a dead parent. Callers that must survive that use
        :meth:`AppSupervisor.surviving_family`, which revalidates each known
        member individually.
        """
        root_identity = self.identity(pid)
        if root_identity is None:
            return []
        psutil = psutil_or_none()
        found = [root_identity]
        if psutil is None:
            return found
        try:
            root = psutil.Process(root_identity[0])
            if root.create_time() != root_identity[1]:
                return found
            descendants = root.children(recursive=True)
        except Exception as exc:
            if isinstance(exc, getattr(psutil, "NoSuchProcess", ())):
                return found
            raise AppExitTimeout("could not verify protected process tree") from exc
        for child in descendants:
            try:
                # The enumerated Process object carries its original birth.
                # A fresh PID lookup could substitute a foreign generation.
                child_identity = (int(child.pid), float(child.create_time()))
            except Exception as exc:
                raise AppExitTimeout("could not verify protected descendant identity") from exc
            if self.identity_matches(*child_identity):
                found.append(child_identity)
        return found

    def tree_pids(self, pid):
        return [identity[0] for identity in self.tree_identities(pid)]

    @staticmethod
    def _identity_map(identities):
        """Snapshot immutable birth records; bare PIDs cannot carry close authority."""
        owned = {}
        for record in identities or ():
            pid, created = record
            pid, created = int(pid), float(created)
            if pid <= 0 or not math.isfinite(created):
                raise ValueError("invalid protected process identity")
            if pid in owned and owned[pid] != created:
                raise ValueError("conflicting protected process identity")
            owned[pid] = created
        return owned

    def _pin_process(self, pid, created, destructive=False):
        pin = _IdentityHandle(pid, destructive)
        try:
            if pin.matches(created) and self.identity_matches(pid, created):
                return pin
        except Exception:
            pin.close()
            raise
        pin.close()
        return None

    def request_close(self, pid, identities=None):
        """WM_CLOSE only to windows whose owner's recorded birth still matches."""
        import ctypes
        import sa_win
        from ctypes import wintypes
        if identities is None:
            return 0  # A remembered bare root PID is not close authority.
        owned = self._identity_map(identities)
        if not owned:
            return 0
        WM_CLOSE = 0x0010
        sent = [0]
        pins = {}

        def callback(hwnd, _lparam):
            owner = wintypes.DWORD()
            sa_win.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            member = int(owner.value)
            if member not in owned or not sa_win.IsWindowVisible(hwnd):
                return True
            expected = owned[member]
            try:
                pin = pins.get(member)
                if pin is None:
                    pin = self._pin_process(member, expected)
                    if pin is None:
                        return True
                    pins[member] = pin
                # Re-read for each window, including after potentially slow
                # enumeration. Holding the object also closes the check/action
                # PID-lookup race; never close via a newly resolved process.
                if not pin.matches(expected) or not self.identity_matches(member, expected):
                    return True
                sa_win.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if (owner.value == member and pin.matches(expected) and
                        self.identity_matches(member, expected) and
                        sa_win.PostMessageW(hwnd, WM_CLOSE, 0, 0)):
                    sent[0] += 1
            except Exception:
                pass  # Unknown/changed identity has no destructive authority.
            return True

        try:
            if sa_win.IS_WINDOWS:
                sa_win.EnumWindows(sa_win.WNDENUMPROC(callback), 0)
        except Exception:
            return sent[0]
        finally:
            for pin in pins.values():
                pin.close()
        return sent[0]

    def terminate_pids(self, identities):
        """Bind births and native objects; revalidate before terminate AND kill."""
        psutil = psutil_or_none()
        if psutil is None:
            return
        owned = self._identity_map(identities)
        victims, pins = [], {}
        try:
            for pid, created in sorted(owned.items()):
                try:
                    proc = psutil.Process(pid)
                    if proc.create_time() != created:
                        continue
                    pin = self._pin_process(pid, created, destructive=True)
                    if pin is None:
                        continue
                    pins[pid] = pin
                    if pin.matches(created) and self.identity_matches(pid, created):
                        pin.terminate()
                        victims.append(proc)
                except Exception:
                    continue
            if not victims:
                return
            _gone, alive = psutil.wait_procs(victims, timeout=5)
            for proc in alive:
                try:
                    expected = owned[proc.pid]
                    pin = pins[proc.pid]
                    if pin.matches(expected) and self.identity_matches(proc.pid, expected):
                        pin.kill()
                except Exception:
                    pass
        finally:
            for pin in pins.values():
                pin.close()

    def terminate_tree(self, pid, created=None):
        if created is not None and self.identity_matches(pid, created):
            identities = self.tree_identities(pid)
            if self.identity_matches(pid, created):
                self.terminate_pids(identities)

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
    """Deterministic adapter for lifecycle tests. Spawns nothing.

    It models the Windows behaviour the real adapter has: pids are reusable
    and a child outlives its parent. :meth:`reuse_pid` exists so a test can
    hand a dead pid to a brand new process and prove the supervisor treats
    the newcomer as a stranger.
    """

    #: Distinct fake creation times, so a reused pid is visibly a new process.
    BIRTH = 1000.0

    def __init__(self):
        self.next_pid = 4100
        self.alive_pids = set()
        self.trees = {}
        self.births = {}
        self.spawned = []
        self.close_requests = []
        self.terminations = []
        self.fail_spawn = None
        self._foreground = 0
        self.ignore_close = False
        self._birth_serial = 0

    def _new_birth(self):
        self._birth_serial += 1
        return float(self.BIRTH + self._birth_serial)

    def spawn(self, executable, arguments, working_directory):
        self.spawned.append((executable, list(arguments), working_directory))
        if self.fail_spawn:
            raise AppLaunchError(self.fail_spawn)
        pid = self.next_pid
        self.next_pid += 1
        self.alive_pids.add(pid)
        self.trees[pid] = [pid, pid + 1000]
        self.alive_pids.add(pid + 1000)
        self.births[pid] = self._new_birth()
        self.births[pid + 1000] = self._new_birth()
        return pid

    def alive(self, pid):
        return int(pid) in self.alive_pids

    def identity(self, pid):
        pid = int(pid or 0)
        if pid not in self.alive_pids:
            return None
        return (pid, float(self.births.get(pid, self.BIRTH)))

    def identity_matches(self, pid, created):
        identity = self.identity(pid)
        if identity is None:
            return False
        try:
            return math.isfinite(float(created)) and identity[1] == float(created)
        except (TypeError, ValueError):
            return False

    def tree_identities(self, pid):
        if self.identity(pid) is None:
            return []
        found = []
        for member in self.trees.get(int(pid), [int(pid)]):
            identity = self.identity(member)
            if identity is not None:
                found.append(identity)
        return found

    def tree_pids(self, pid):
        return [identity[0] for identity in self.tree_identities(pid)]

    def request_close(self, pid, identities=None):
        if identities is None:
            return 0
        sent = 0
        for member, created in self._identity_map(identities).items():
            if self.identity_matches(member, created):
                self.close_requests.append(member)
                sent += 1
                if not self.ignore_close:
                    self.alive_pids.discard(member)
        return sent

    def terminate_pids(self, identities):
        for pid, created in sorted(self._identity_map(identities).items()):
            if self.identity_matches(pid, created):
                self.terminations.append(pid)
                self.alive_pids.discard(pid)

    def terminate_tree(self, pid, created=None):
        if created is not None and self.identity_matches(pid, created):
            identities = self.tree_identities(pid)
            if self.identity_matches(pid, created):
                self.terminate_pids(identities)

    def exit_tree(self, pid):
        for p in list(self.trees.get(int(pid), [int(pid)])):
            self.alive_pids.discard(p)

    def exit_parent_only(self, pid):
        """A child outlives the parent -- the Electron case the design fears."""
        self.alive_pids.discard(int(pid))

    def reuse_pid(self, pid, tree=(None,)):
        """Hand a dead pid to a fresh process. Pid reuse, with a new birth."""
        pid = int(pid)
        # A new foreign generation does not remain a descendant through a
        # stale fake parent->PID edge. Match the real OS ancestry semantics.
        for parent in self.trees:
            self.trees[parent] = [p for p in self.trees[parent] if p != pid]
        self.alive_pids.add(pid)
        self.births[pid] = self._new_birth()
        members = [pid]
        for index, extra in enumerate(tree):
            if extra is None:
                extra = pid + 2000 + index
            self.alive_pids.add(int(extra))
            self.births[int(extra)] = self._new_birth()
            members.append(int(extra))
        self.trees[pid] = members
        return members

    def set_foreground(self, pid):
        self._foreground = int(pid or 0)

    def foreground_pid(self):
        return self._foreground


class ProcessObservation(object):
    """Ephemeral per‑turn snapshot: one tree + one foreground lookup."""
    __slots__ = ("root_pid", "tree_pids", "foreground_pid", "running", "foreground_protected", "identities")

    def __init__(self, root_pid, tree_pids, foreground_pid, running, foreground_protected, identities=()):
        self.root_pid = root_pid
        self.tree_pids = tree_pids
        self.foreground_pid = foreground_pid
        self.running = running
        self.foreground_protected = foreground_protected
        self.identities = tuple(identities)


class AppSupervisor(object):
    """Tracks one protected application instance for one profile.

    The tracked identity is :attr:`family` -- every ``(pid, creation_time)``
    this supervisor has ever seen alive, pruned by revalidation rather than
    by the death of an ancestor. Discovery walks the launched root and every
    surviving member, so a renderer that spawns a GPU process after the parent
    is gone is still picked up.
    """

    def __init__(self, adapter=None, clock=time.monotonic, sleep=time.sleep):
        self.adapter = adapter or ProcessAdapter()
        self.clock = clock
        self.sleep = sleep
        self.pid = 0
        self.launched_at = None
        self._family = {}

    # -- launch -----------------------------------------------------------
    def conflicting_instances(self, executable):
        """Unmanaged processes already running this application's executable.

        Electron applications are single-instance per USER-DATA DIRECTORY, and
        SAITULS deliberately does not override it. So when an operator already
        has Obsidian open, a protected launch does not produce a protected
        process: the new process hands its arguments to the running one and
        exits, and the application that ends up reading the vault is one
        SAITULS did not start, does not own, and cannot detach from when the
        vault has to be relocked.

        The response is refusal, never adoption. Adopting would mean claiming
        protected status for a process whose lifetime, window and files are
        the operator's, and treating its exit as an exit of the protected
        application -- which is the exact mistake the process-identity work
        exists to prevent.

        Measured live against the operator's own Obsidian build: a second
        launch sharing the user-data directory exits immediately and leaves the
        first instance's four processes untouched.
        """
        name = os.path.basename(str(executable or "")).lower()
        if not name:
            return []
        ours = set(self.surviving_family())
        found = []
        try:
            import psutil
        except ImportError:
            return []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pid = int(proc.info["pid"])
                if pid in ours or pid == os.getpid():
                    continue
                if (proc.info.get("name") or "").lower() == name:
                    found.append(pid)
            except Exception:
                continue
        return sorted(found)

    def launch(self, executable, arguments, working_directory):
        conflicts = self.conflicting_instances(executable)
        if conflicts:
            raise AppLaunchError(
                "%s is already running (pid %s) outside SAITULS. Electron "
                "hands a second launch to the running instance and exits, so "
                "the vault would be opened by a process SAITULS does not own "
                "and could not detach from at relock. Close %s completely "
                "(including the tray icon) and launch it again from here."
                % (os.path.basename(str(executable)),
                   ", ".join(str(p) for p in conflicts),
                   os.path.basename(str(executable))),
                "single_instance_conflict")
        pid = self.adapter.spawn(executable, arguments, working_directory)
        self.pid = int(pid)
        self.launched_at = self.clock()
        self._family = {}
        self._seed_root()
        self.surviving_family()
        return self.pid

    def attach(self, pid):
        self.pid = int(pid or 0)
        self._family = {}
        self._seed_root()
        self.surviving_family()

    def forget(self):
        self.pid = 0
        self.launched_at = None
        self._family = {}

    # -- observation ------------------------------------------------------
    def _seed_root(self):
        identity = self.adapter.identity(self.pid)
        if identity is not None:
            self._family[int(identity[0])] = float(identity[1])

    def surviving_family(self):
        """Discover, merge, revalidate. Returns the live protected pids.

        Discovery alone is not enough and never was: ``psutil.Process(root)``
        raises the moment the Electron parent exits, and returning ``[]``
        there is exactly how a live renderer ends up looking like a stopped
        application. So every known member is revalidated on its own
        identity, and a member stays in the family while it stays alive --
        whether or not the process that spawned it still exists.

        While the root lives, one recursive walk covers the whole tree. Once
        it is gone the walk moves to each survivor, so a grandchild born after
        the parent's death is still discovered instead of going untracked.
        """
        adapter = self.adapter
        if not self.pid:
            return []
        # Prune BEFORE using a member as a discovery root. Otherwise a reused
        # root can enumerate its foreign tree and overwrite the old birth.
        for member, created in list(self._family.items()):
            if not adapter.identity_matches(member, created):
                del self._family[member]
        if self.pid in self._family:
            discovery_roots = [self.pid]
        else:
            discovery_roots = sorted(self._family)
        for pid in discovery_roots:
            expected = self._family.get(pid)
            identities = adapter.tree_identities(pid) or ()
            if not adapter.identity_matches(pid, expected):
                continue
            for identity in identities:
                try:
                    member, created = identity
                except (TypeError, ValueError):
                    continue
                if member and created is not None:
                    if int(member) in self._family and self._family[int(member)] != float(created):
                        continue
                    self._family[int(member)] = float(created)
        for pid in list(self._family):
            if not adapter.identity_matches(pid, self._family[pid]):
                del self._family[pid]
        return sorted(self._family)

    @property
    def family(self):
        """``(pid, creation_time)`` pairs believed alive right now."""
        return dict((pid, created) for pid, created in self._family.items())

    @property
    def running(self):
        return bool(self.pid) and bool(self.surviving_family())

    def tree(self):
        return self.surviving_family() if self.pid else []

    def observation(self):
        """One snapshot per profile per periodic turn (SRC-027 PERF-004).

        Captures the tree once and optionally the foreground pid; subsequent
        derives reuse this instead of re‑enumerating.
        """
        if not self.pid:
            return ProcessObservation(self.pid, [], None, False, False)
        tree = self.surviving_family()
        running = bool(tree)
        fg = self.adapter.foreground_pid()
        identities = tuple(sorted(self._family.items()))
        protected = (bool(fg) and int(fg) in self._family and
                     self.adapter.identity_matches(int(fg), self._family[int(fg)])) if running else False
        return ProcessObservation(self.pid, tree, fg, running, protected, identities)

    def running_from(self, obs):
        if isinstance(obs, ProcessObservation):
            return obs.root_pid == self.pid and any(
                self._family.get(pid) == created and self.adapter.identity_matches(pid, created)
                for pid, created in obs.identities)
        return self.running

    def foreground_is_protected_from(self, obs):
        """Same as foreground_is_protected() but reuses a prior snapshot."""
        if not self.pid:
            return False
        if isinstance(obs, ProcessObservation):
            if obs.root_pid != self.pid or not obs.foreground_protected:
                return False
            expected = dict(obs.identities).get(obs.foreground_pid)
            return (expected is not None and self._family.get(obs.foreground_pid) == expected and
                    self.adapter.identity_matches(obs.foreground_pid, expected))
        return self.foreground_is_protected()

    def foreground_is_protected(self):
        """True only when a surviving protected process owns the foreground."""
        if not self.pid:
            return False
        fg = self.adapter.foreground_pid()
        if not fg:
            return False
        self.surviving_family()
        expected = self._family.get(int(fg))
        return expected is not None and self.adapter.identity_matches(int(fg), expected)

    # -- shutdown ---------------------------------------------------------
    def request_graceful_close(self):
        if not self.pid:
            return 0
        # Close the whole validated family, not the tree under a root that may
        # already be gone: WM_CLOSE to a dead parent's subtree reaches nobody.
        self.surviving_family()
        return self.adapter.request_close(self.pid, tuple(sorted(self._family.items())))

    def wait_for_exit(self, timeout, poll_interval=0.25):
        """Wait for the whole family. Returns True when every member is gone."""
        if not self.pid:
            return True
        deadline = self.clock() + max(0.0, float(timeout))
        while True:
            if not self.surviving_family():
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(poll_interval)

    def terminate(self):
        if self.pid:
            self.surviving_family()
            self.adapter.terminate_pids(tuple(sorted(self._family.items())))

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
