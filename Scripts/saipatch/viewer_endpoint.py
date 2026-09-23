"""
viewer_endpoint.py - Strict loopback OpenCode endpoint discovery for the
Queue Viewer Cockpit.

V1 contract:
- Loopback ONLY. Accepted listener addresses are exactly 127.0.0.1 and ::1.
  Wildcard listeners (0.0.0.0 / ::) are NOT loopback listeners and are
  rejected outright.
- ::1 listeners are probed through http://[::1]:PORT, never via 127.0.0.1.
- Process authority: a candidate must expose the exact socket-owner PID, a
  verified executable path from psutil, and a process name containing
  "opencode". If an installed OpenCode authority path can be resolved
  (env SAIPATCH_OPENCODE_EXE or PATH), the executable must match it exactly.
  A process is never accepted merely because its name contains "opencode".
- Snapshot authority: discovery produces immutable ProcessSnapshot records
  (pid, start_time, executable_path, endpoint, verified_session_ids) which
  are fed to the observer through a queued signal.
- Exact-session ownership: /api/session/active is ACTIVITY EVIDENCE ONLY.
  A selected session is owned by an endpoint only if the pinned supported
  native API GET /api/session/{sessionID} returns that exact session on that
  endpoint. The session does NOT have to be RUNNING (idle sessions are valid
  send targets). Candidates are never guessed between.
- All control from the GUI flows through queued Qt signals; discovery runs
  in its own QThread.
"""
import json
import os
import shutil
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional, List, Set

try:
    import psutil
except ImportError:
    psutil = None

from PyQt6.QtCore import QObject, pyqtSignal, QTimer, pyqtSlot

# The ONLY listener addresses accepted by the V1 contract. Wildcards
# (0.0.0.0 / ::) are deliberately absent: they are not loopback listeners.
ALLOWED_LISTENERS = ("127.0.0.1", "::1")


def is_allowed_listener(ip: str) -> bool:
    """Wildcard/listener gate. Only strict loopback is accepted."""
    return ip in ALLOWED_LISTENERS


def listener_probe_url(ip: str, port: int) -> str:
    """Build the probe URL for a verified loopback listener.

    ::1 must be probed through http://[::1]:PORT (bracketed IPv6 literal),
    never through 127.0.0.1.
    """
    if ip == "::1":
        return f"http://[::1]:{port}"
    return f"http://{ip}:{port}"


def expected_opencode_executable() -> Optional[str]:
    """Resolve the installed OpenCode executable authority path, if available.

    Sources, in order:
      1. SAIPATCH_OPENCODE_EXE environment variable (explicit authority).
      2. shutil.which("opencode.exe") / which("opencode") on PATH.

    Returns a normalized absolute path or None when no authority resolves.
    """
    env = os.environ.get("SAIPATCH_OPENCODE_EXE")
    if env and os.path.isfile(env):
        return os.path.normcase(os.path.abspath(env))
    for name in ("opencode.exe", "opencode"):
        found = shutil.which(name)
        if found and os.path.isfile(found):
            return os.path.normcase(os.path.abspath(found))
    return None


def verify_executable(executable_path: Optional[str], expected: Optional[str]) -> bool:
    """Executable-path authority gate.

    Rules:
    - The psutil-reported executable path must exist (never None/empty).
    - Its basename must contain "opencode" (case-insensitive).
    - When an expected authority path resolves, it must match exactly
      (normalized). A renamed imposter or a random "opencode"-named process
      with a foreign executable is rejected.
    """
    if not executable_path:
        return False
    norm = os.path.normcase(os.path.abspath(executable_path))
    if not os.path.isfile(norm):
        return False
    if "opencode" not in os.path.basename(norm).lower():
        return False
    if expected and norm != expected:
        return False
    return True


@dataclass(frozen=True)
class ProcessSnapshot:
    """Immutable process/endpoint discovery record.

    Liveness for a session may ONLY be derived from a snapshot whose
    verified_session_ids contains that exact session ID.
    """
    pid: int
    start_time: float
    executable_path: str
    endpoint: str
    verified_session_ids: tuple = field(default=())


@dataclass(frozen=True)
class ResolvedEndpoint:
    pid: int
    start_time: float
    url: str
    executable_path: str
    sessions: tuple = field(default=())


@dataclass(frozen=True)
class ResolutionRequest:
    """Immutable resolver request identity.

    Every session selection increments the generation; a resolver result is
    accepted by the GUI only when its (generation, session_id) both match the
    current selection. This kills the stale-result race where a resolution
    started for session A arrives after the GUI selected session B.
    """
    generation: int
    session_id: Optional[str]


@dataclass(frozen=True)
class EndpointStatus:
    state: str  # 'RESOLVED', 'AMBIGUOUS', 'UNAVAILABLE', 'PROBING'
    endpoint: Optional[ResolvedEndpoint]
    error: str
    generation: int = 0
    session_id: Optional[str] = None


class OpenCodeEndpointResolver(QObject):
    """Discovers loopback OpenCode endpoints in its own worker thread.

    Control contract: the GUI NEVER calls set_target_session()/stop()
    directly. It emits select_session()/stop_worker(), which are connected
    (after moveToThread) to the queued slots below.
    """
    endpoint_resolved = pyqtSignal(object)    # EndpointStatus
    process_snapshots = pyqtSignal(object)    # List[ProcessSnapshot]

    # Queued command signals (GUI -> worker)
    select_session = pyqtSignal(str)
    resolve_now = pyqtSignal()
    stop_worker = pyqtSignal()

    BACKOFF_SEQUENCE = [1.0, 2.0, 5.0, 10.0]
    PROBE_TIMEOUT = 2.0
    RESOLVE_INTERVAL = 2000

    def __init__(self, generation_provider=None):
        super().__init__()
        self._cached: Optional[ResolvedEndpoint] = None
        self._backoff_index = 0
        self._target_session_id: Optional[str] = None
        self._running = False
        self._timer: Optional[QTimer] = None
        # Request identity: the GUI provides a callable returning the current
        # generation (incremented on every selection); tests may inject.
        self._generation_provider = generation_provider or (lambda: 0)

    # ── Lifecycle (worker thread only) ──────────────────────────────

    @pyqtSlot()
    def start(self):
        """Runs in the worker thread after moveToThread + thread.started."""
        if self._running:
            return
        self._running = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._resolve)
        self._timer.setSingleShot(True)
        self._timer.start(0)

    @pyqtSlot()
    def _on_stop(self):
        """Queued stop: the worker stops its own timer in its own thread."""
        self._running = False
        if self._timer:
            self._timer.stop()
            self._timer = None

    @pyqtSlot(str)
    def _on_select_session(self, session_id: str):
        """Queued selection command. Empty string clears the target."""
        target = session_id or None
        if target != self._target_session_id:
            self._target_session_id = target
            self._cached = None
            self._backoff_index = 0
        if self._running and self._timer:
            self._timer.stop()
            self._timer.start(0)

    @pyqtSlot()
    def _on_resolve_now(self):
        """F5: force an immediate resolution tick in the worker thread."""
        if self._running and self._timer:
            self._timer.stop()
            self._timer.start(0)

    # Backwards-compatible aliases used by tests/driver code that calls the
    # worker in its own thread (never from the GUI thread).
    def set_target_session(self, session_id: Optional[str]):
        self._on_select_session(session_id or "")

    def stop(self):
        self._on_stop()

    # ── Resolution loop (worker thread) ─────────────────────────────

    def _resolve(self):
        if not self._running:
            return

        status = self._do_resolve()
        self.endpoint_resolved.emit(status)

        if not self._running or not self._timer:
            return

        if status.state == 'RESOLVED':
            self._backoff_index = 0
            self._timer.start(self.RESOLVE_INTERVAL)
        else:
            backoff_sec = self.BACKOFF_SEQUENCE[self._backoff_index]
            self._backoff_index = min(self._backoff_index + 1, len(self.BACKOFF_SEQUENCE) - 1)
            self._timer.start(int(backoff_sec * 1000))

    def _scan_candidates(self) -> List[ProcessSnapshot]:
        """Scan processes for verified loopback OpenCode HTTP listeners.

        Returns immutable snapshot records. Overridable in tests.
        """
        if psutil is None:
            return []

        expected_exe = expected_opencode_executable()
        snapshots: List[ProcessSnapshot] = []
        for proc in psutil.process_iter(['name', 'exe', 'create_time', 'pid']):
            try:
                name = (proc.info.get('name') or '').lower()
                if 'opencode' not in name:
                    continue

                exe = proc.info.get('exe')
                if not verify_executable(exe, expected_exe):
                    # Spoofed/random executable authority: rejected.
                    continue

                pid = proc.info['pid']
                start_time = proc.info['create_time']

                for conn in proc.net_connections(kind='tcp'):
                    if conn.status != 'LISTEN' or conn.laddr is None:
                        continue
                    ip = conn.laddr.ip
                    port = conn.laddr.port

                    if not is_allowed_listener(ip):
                        # Wildcard (0.0.0.0 / ::) and non-loopback listeners
                        # are rejected by the V1 contract.
                        continue

                    url = listener_probe_url(ip, port)
                    verified = self._probe_endpoint(url)
                    if verified:
                        snapshots.append(ProcessSnapshot(
                            pid=pid,
                            start_time=start_time,
                            executable_path=os.path.normcase(os.path.abspath(exe)),
                            endpoint=url,
                            verified_session_ids=tuple(verified),
                        ))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return snapshots

    def _do_resolve(self) -> EndpointStatus:
        generation = self._generation_provider()
        snapshots = self._scan_candidates()

        # Feed the snapshot authority to the observer (queued cross-thread).
        self.process_snapshots.emit(list(snapshots))

        candidates: List[ResolvedEndpoint] = []
        for snap in snapshots:
            if (self._target_session_id
                    and self._target_session_id not in snap.verified_session_ids):
                continue
            candidates.append(ResolvedEndpoint(
                pid=snap.pid,
                start_time=snap.start_time,
                url=snap.endpoint,
                executable_path=snap.executable_path,
                sessions=tuple(snap.verified_session_ids),
            ))

        if not candidates:
            return EndpointStatus('UNAVAILABLE', None, 'No verified OpenCode endpoints found',
                                  generation=generation, session_id=self._target_session_id)

        if len(candidates) > 1:
            return EndpointStatus('AMBIGUOUS', None, 'Multiple OpenCode endpoints found',
                                  generation=generation, session_id=self._target_session_id)

        self._cached = candidates[0]
        return EndpointStatus('RESOLVED', self._cached, '',
                              generation=generation, session_id=self._target_session_id)

    # ── HTTP probing (worker thread) ────────────────────────────────

    def _probe_endpoint(self, url: str) -> Optional[List[str]]:
        """Probe a candidate endpoint and return verified session IDs.

        /api/session/active is activity evidence only. Exact ownership of a
        session is proven by GET /api/session/{id} returning 200 with that
        session's JSON (the pinned supported native API of the supported
        build). An idle session still verifies: no RUNNING requirement.
        """
        sessions: Set[str] = set()
        saw_opencode_api = False

        # Optional activity evidence (never sufficient for ownership).
        active = self._get_json(f"{url}/api/session/active")
        if active is not None:
            saw_opencode_api = True
            for sid in extract_session_ids(active):
                sessions.add(sid)

        target = self._target_session_id
        if target:
            body = self._get_json(f"{url}/api/session/{target}")
            if body is None:
                # Exact-session GET failed: this endpoint does NOT prove
                # ownership of the selected session.
                return None
            saw_opencode_api = True
            found = extract_session_ids(body)
            if target not in found:
                return None
            sessions.add(target)

        if not saw_opencode_api:
            return None
        return sorted(sessions)

    def _get_json(self, url: str) -> Optional[dict]:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=self.PROBE_TIMEOUT) as response:
                if response.status != 200:
                    return None
                data = json.loads(response.read().decode('utf-8'))
                return data if isinstance(data, dict) else None
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
            return None

    def _is_pid_valid(self, pid: int, expected_start_time: float) -> bool:
        if psutil is None:
            return False
        try:
            proc = psutil.Process(pid)
            return abs(proc.create_time() - expected_start_time) <= 1.0
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def get_cached_endpoint(self) -> Optional[ResolvedEndpoint]:
        return self._cached


def extract_session_ids(payload: dict) -> List[str]:
    """Extract session IDs from an OpenCode session JSON shape."""
    out: List[str] = []
    data = payload.get('data', payload)
    if isinstance(data, dict):
        if isinstance(data.get('id'), str):
            out.append(data['id'])
        inner = data.get('sessions')
        if isinstance(inner, list):
            for s in inner:
                if isinstance(s, dict) and isinstance(s.get('id'), str):
                    out.append(s['id'])
    elif isinstance(data, list):
        for s in data:
            if isinstance(s, dict) and isinstance(s.get('id'), str):
                out.append(s['id'])
    return out
