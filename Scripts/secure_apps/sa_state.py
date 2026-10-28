"""Durable, non-secret Secure Apps state and crash reconciliation.

The state file records facts, never secrets: which profile, which container,
which mount path, the state the broker believed it was in, the last
transition, and the last protected process id. It exists for exactly one
reason -- a broker that dies does not take the truth with it.

The reconciliation rule is deliberately pessimistic, because the opposite
mistake is the dangerous one:

    a crashed broker does NOT imply a locked vault.

So a recorded state of MOUNTED/RUNNING with no live broker is not "probably
unmounted by Windows"; it is RECOVERY_REQUIRED until the backend has been
asked. Equally, a recorded LOCKED with a volume that the backend still
reports as mounted is RECOVERY_REQUIRED, not "the file must be stale".
"""
import json
import ntpath
import os
import tempfile
import threading
import time

STATE_SCHEMA = "saituls.secure-apps.state/1"
STATE_SCHEMA_VERSION = 1

# Public lifecycle states. These are the strings the GUI renders verbatim.
LOCKED = "LOCKED"
AUTH_REQUIRED = "AUTH_REQUIRED"
AUTHENTICATING = "AUTHENTICATING"
UNLOCKING = "UNLOCKING"
MOUNTED = "MOUNTED"
RUNNING = "RUNNING"
SESSION_CACHED = "SESSION_CACHED"
LOCKING = "LOCKING"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
ERROR = "ERROR"

ALL_STATES = (LOCKED, AUTH_REQUIRED, AUTHENTICATING, UNLOCKING, MOUNTED,
              RUNNING, SESSION_CACHED, LOCKING, RECOVERY_REQUIRED, ERROR)

# States in which the protected filesystem is, or may be, attached.
STORAGE_EXPOSED_STATES = (UNLOCKING, MOUNTED, RUNNING, LOCKING, RECOVERY_REQUIRED)

ENTRY_FIELDS = ("profile_id", "container_path", "mount_path", "expected_state",
                "last_transition", "last_pid", "broker_pid", "broker_nonce",
                "broker_birth_time", "updated")


class StateError(Exception):
    pass


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ProfileState(object):
    __slots__ = ENTRY_FIELDS

    def __init__(self, profile_id, container_path="", mount_path="",
                 expected_state=LOCKED, last_transition="init", last_pid=0,
                 broker_pid=0, broker_nonce="", broker_birth_time=0.0, updated=None):
        self.profile_id = profile_id
        self.container_path = container_path
        self.mount_path = mount_path
        self.expected_state = expected_state
        self.last_transition = last_transition
        self.last_pid = int(last_pid or 0)
        self.broker_pid = int(broker_pid or 0)
        self.broker_nonce = str(broker_nonce or "")
        self.broker_birth_time = float(broker_birth_time or 0.0)
        self.updated = updated or _now_iso()

    def to_dict(self):
        return {name: getattr(self, name) for name in ENTRY_FIELDS}

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            raise StateError("profile state entry must be an object")
        pid = raw.get("profile_id")
        if not isinstance(pid, str) or not pid:
            raise StateError("profile state entry has no profile_id")
        state = raw.get("expected_state", LOCKED)
        if state not in ALL_STATES:
            # An unrecognised recorded state is not trusted as LOCKED.
            state = RECOVERY_REQUIRED
        return cls(
            profile_id=pid,
            container_path=str(raw.get("container_path") or ""),
            mount_path=str(raw.get("mount_path") or ""),
            expected_state=state,
            last_transition=str(raw.get("last_transition") or "unknown"),
            last_pid=int(raw.get("last_pid") or 0),
            broker_pid=int(raw.get("broker_pid") or 0),
            broker_nonce=str(raw.get("broker_nonce") or ""),
            broker_birth_time=float(raw.get("broker_birth_time") or 0.0),
            updated=str(raw.get("updated") or _now_iso()),
        )


class StateStore(object):
    """Atomic JSON store for :class:`ProfileState` rows. Never holds a secret."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._entries = {}
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self):
        with self._lock:
            self._entries = {}
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    document = json.load(handle)
            except FileNotFoundError:
                return self._entries
            except (json.JSONDecodeError, OSError):
                # A corrupt state file is a recovery condition, not a reason to
                # believe everything is locked. Entries stay empty and the
                # caller's reconcile() asks the backend for the truth.
                return self._entries
            if not isinstance(document, dict):
                return self._entries
            if document.get("schema") != STATE_SCHEMA:
                return self._entries
            for raw in document.get("profiles", []) or []:
                try:
                    entry = ProfileState.from_dict(raw)
                except StateError:
                    continue
                self._entries[entry.profile_id] = entry
            return self._entries

    def save(self):
        with self._lock:
            document = {
                "schema": STATE_SCHEMA,
                "schema_version": STATE_SCHEMA_VERSION,
                "updated": _now_iso(),
                "profiles": [e.to_dict() for e in self._entries.values()],
            }
            directory = ntpath.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=directory or None, prefix=".secure-apps-", suffix=".tmp")
            try:
                json.dump(document, handle, indent=2, sort_keys=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
            os.replace(handle.name, self.path)

    # -- accessors --------------------------------------------------------
    def get(self, profile_id):
        with self._lock:
            return self._entries.get(profile_id)

    def all(self):
        with self._lock:
            return dict(self._entries)

    def record(self, profile_id, container_path, mount_path, expected_state,
               transition, last_pid=0, broker_pid=None, broker_nonce=None,
               broker_birth_time=None):
        if expected_state not in ALL_STATES:
            raise StateError("unknown expected_state: %r" % (expected_state,))
        with self._lock:
            entry = self._entries.get(profile_id)
            if entry is None:
                entry = ProfileState(profile_id)
                self._entries[profile_id] = entry
            entry.container_path = container_path
            entry.mount_path = mount_path
            entry.expected_state = expected_state
            entry.last_transition = transition
            entry.last_pid = int(last_pid or 0)
            if broker_pid is not None:
                # The owner identity is rewritten as a unit: a new pid never
                # inherits the previous broker's nonce or birth time.
                entry.broker_pid = int(broker_pid)
                entry.broker_nonce = str(broker_nonce or "")
                entry.broker_birth_time = float(broker_birth_time or 0.0)
            entry.updated = _now_iso()
            self.save()
            return entry


def pid_is_alive(pid):
    if not pid or int(pid) <= 0:
        return False
    try:
        import psutil
    except ImportError:
        return False
    try:
        return psutil.pid_exists(int(pid))
    except Exception:
        return False


def process_birth_time(pid):
    """The process creation time for *pid*, or None when unmeasurable.

    Paired with the broker-instance nonce this defeats PID reuse: a recycled
    PID has a different creation time, so it cannot stand in for the process
    that recorded the exposed storage state.
    """
    if not pid or int(pid) <= 0:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def broker_instance_identity(pid=None, nonce=None, birth_time=None):
    """``(pid, nonce, birth_time)`` for this broker instance.

    A nonce is generated once per broker process; the birth time is measured
    from the kernel. Together with the pid they are the unforgeable identity a
    durable exposed-state record is bound to.
    """
    import os as _os
    import uuid as _uuid
    pid = int(pid if pid is not None else _os.getpid())
    nonce = str(nonce) if nonce else _uuid.uuid4().hex
    birth_time = (float(birth_time) if birth_time is not None
                  else process_birth_time(pid))
    return pid, nonce, birth_time


class Reconciliation(object):
    __slots__ = ("profile_id", "recorded_state", "actual_storage_state",
                 "verdict", "reason", "broker_alive")

    def __init__(self, profile_id, recorded_state, actual_storage_state,
                 verdict, reason, broker_alive):
        self.profile_id = profile_id
        self.recorded_state = recorded_state
        self.actual_storage_state = actual_storage_state
        self.verdict = verdict
        self.reason = reason
        self.broker_alive = broker_alive

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}

    def __repr__(self):
        return "<Reconciliation %s recorded=%s actual=%s -> %s (%s)>" % (
            self.profile_id, self.recorded_state, self.actual_storage_state,
            self.verdict, self.reason)


def reconcile(profile, store, storage_state, broker_alive_check=pid_is_alive,
              broker_nonce=None, broker_birth_time=None):
    """Compare recorded state against the real world. Pessimistic by design.

    *storage_state* is the backend's answer: ``"detached"``,
    ``"attached_locked"`` or ``"mounted"``.

    Ownership of an exposed mount is proven by the broker *instance*, never by
    a bare pid: the durable record is bound to a per-process nonce, and a live
    pid alone (reusable after a crash) can never establish ownership. A legacy
    PID-only record -- or any record whose nonce does not match the asking
    broker -- is treated as orphaned and routed to RECOVERY_REQUIRED.
    """
    entry = store.get(profile.id)
    recorded = entry.expected_state if entry else LOCKED
    broker_alive = False
    if entry and broker_nonce:
        nonce_matches = bool(entry.broker_nonce) and entry.broker_nonce == str(broker_nonce)
        birth_matches = True
        if (entry.broker_birth_time and broker_birth_time):
            try:
                birth_matches = abs(float(entry.broker_birth_time)
                                    - float(broker_birth_time)) <= 1.0
            except (TypeError, ValueError):
                birth_matches = False
        broker_alive = bool(nonce_matches and birth_matches
                            and broker_alive_check(entry.broker_pid))

    if storage_state == "mounted":
        if broker_alive and recorded in (MOUNTED, RUNNING, UNLOCKING, LOCKING):
            verdict = recorded
            reason = "live broker instance owns the mounted volume"
        else:
            verdict = RECOVERY_REQUIRED
            reason = ("protected volume is mounted with no live broker owning it"
                      if not broker_alive else
                      "protected volume is mounted but recorded state was " + recorded)
        return Reconciliation(profile.id, recorded, storage_state, verdict, reason,
                              broker_alive)

    if recorded in STORAGE_EXPOSED_STATES and not broker_alive:
        # Recorded exposure, no live owner, backend says not mounted. The
        # volume is in fact down, but the previous session ended uncleanly and
        # the user is told so rather than being handed a silent green light.
        return Reconciliation(profile.id, recorded, storage_state, RECOVERY_REQUIRED,
                              "previous broker ended without relocking cleanly",
                              broker_alive)

    if storage_state == "attached_locked":
        return Reconciliation(profile.id, recorded, storage_state, RECOVERY_REQUIRED,
                              "container is attached but locked; a previous "
                              "detach did not complete", broker_alive)

    return Reconciliation(profile.id, recorded, storage_state, LOCKED,
                          "container detached; recorded state consistent",
                          broker_alive)
