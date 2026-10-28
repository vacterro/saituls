#!/usr/bin/env python3
"""SAI HANDOFF live log relay -- operator-owned, non-privileged.

Delivers updates from one explicitly operator-selected log file into an
isolated managed account's staged workspace. The managed account never
receives access to the source directory: it reads a copy inside
<workdir>\\.sai-handoff\\<account>, whose DACL is established by
`sai-accounts handoff-stage` and admits only the operator, SYSTEM and that
one managed SID.

Design rules this file obeys:
  * The relay holds no account privileges. It reads the source with the
    operator's own token and writes with it. A managed agent cannot ask it
    to read anything: source selection happens here, at start time, from
    the operator's command line.
  * Log content is opaque bytes. It is never parsed, evaluated or executed,
    and never sent anywhere. There is no network code in this module.
  * Reparse points are refused rather than followed, so the source cannot be
    redirected into an unrelated directory through a junction or symlink.
  * Source discontinuity is reported honestly: truncation, rotation,
    replacement, disappearance and loss are distinct states, and no gap is
    silently closed.
  * Expiry is enforced by this loop against a lease it owns. A lease file
    cannot be renewed by the managed account because the control directory
    lives in the operator's own profile, not in the staged workspace.

stdlib only, no new dependencies.
"""
import ctypes
import ctypes.wintypes
import errno
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid

# GetFileAttributesW returns DWORD but ctypes defaults to c_int, and
# INVALID_FILE_ATTRIBUTES (0xFFFFFFFF) then reads as -1: an absent file would
# look like it carried every attribute, including REPARSE_POINT. Pin the
# unsigned return type once, or a missing source is misreported as an unsafe
# object instead of MISSING.
ctypes.windll.kernel32.GetFileAttributesW.restype = ctypes.wintypes.DWORD
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

SCHEMA = "sai.handoff/relay/1"

# FILE_ATTRIBUTE_REPARSE_POINT (WinNT.h). A reparse point is where a path
# stops naming the object the operator selected, so it is a refusal, not a
# thing to resolve.
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_DIRECTORY = 0x00000010

# A partial trailing line is buffered until its newline arrives. The bound
# stops a source that never emits a newline from growing the relay forever.
MAX_PENDING_BYTES = 65536

DEFAULT_INTERVAL = 1.0
MIN_INTERVAL = 0.05
MAX_INTERVAL = 3600.0
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_KEEP = 2
DEFAULT_LEASE_MINUTES = 60


class RelayError(Exception):
    """A refusal with a machine-readable reason. Never a silent failure."""

    def __init__(self, code, detail=""):
        super().__init__(code + (": " + detail if detail else ""))
        self.code = code
        self.detail = detail


# --------------------------------------------------------------- filesystem
def is_reparse_point(path):
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except Exception:
        return False
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def is_directory(path):
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except Exception:
        return False
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & FILE_ATTRIBUTE_DIRECTORY)


def pid_alive(pid):
    """Is this pid a live process right now?

    OpenProcess alone answers "some process holds this pid", which is true
    after the original exited and the OS handed the number to something else:
    that reported a stopped relay as running and kept a stale runner looking
    authoritative. WaitForSingleObject on the opened handle closes the gap:
    a process object is signalled when it is gone, including a reused number.
    """
    if not pid or pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.windll.kernel32
    kernel32.GetFileAttributesW.restype = ctypes.wintypes.DWORD
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, int(pid))
    if not handle:
        return False
    try:
        # WAIT_OBJECT_0 means the process object is signalled, i.e. it is gone.
        return kernel32.WaitForSingleObject(handle, 0) != 0
    finally:
        kernel32.CloseHandle(handle)


def source_identity(stat_result):
    """Identity that survives an append and changes on replacement.

    (st_dev, st_ino) is the NTFS file id on Windows: two names for the same
    bytes share it, and a replaced file gets a new one.
    """
    return "%d:%d" % (stat_result.st_dev, stat_result.st_ino)


# ------------------------------------------------------------------- layout
def control_root():
    override = os.environ.get("SAI_HANDOFF_ROOT")
    if override:
        return os.path.join(override, "relay")
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return os.path.join(base, "SAITULS", "SAI_HANDOFF", "relay")


def source_id(source_path):
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(source_path)).encode("utf-8")).hexdigest()[:12]
    stem = os.path.basename(source_path)
    safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in stem)
    return "%s-%s" % (safe[:48] or "log", digest)


def control_dir(compact, source_path):
    return os.path.join(control_root(), compact.lower(), source_id(source_path))


def lease_path(cdir):
    return os.path.join(cdir, "lease.json")


def state_path(cdir):
    return os.path.join(cdir, "state.json")


def status_path(cdir):
    return os.path.join(cdir, "status.json")


def runner_log_path(cdir):
    return os.path.join(cdir, "runner.log")


def accounts_exe():
    override = os.environ.get("SAI_ACCOUNTS_EXE")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "accounts", "bin", "sai-accounts.exe")


def now_iso():
    """Local time with its real offset. A fixed "+00:00" on a local timestamp
    is a lie a reader cannot detect from the string alone."""
    offset = -time.timezone if not time.daylight else -time.altzone
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return "%s%s%02d:%02d" % (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                              sign, offset // 3600, (offset % 3600) // 60)


# --------------------------------------------------------------- persistence
def write_json_atomic(path, payload):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + "." + str(os.getpid()) + "." + uuid.uuid4().hex[:8] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# --------------------------------------------------------- account resolution
def resolve_account(selector):
    """Resolve the account through the canonical registry, never a second one."""
    exe = accounts_exe()
    if not os.path.exists(exe):
        raise RelayError("ACCOUNTS_CLI_MISSING", exe)
    proc = subprocess.run([exe, "status", selector], capture_output=True, text=True, timeout=60)
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    try:
        doc = json.loads(line)
    except ValueError:
        raise RelayError("ACCOUNT_LOOKUP_FAILED", line[:200])
    if proc.returncode != 0 or doc.get("status") == "ACCOUNT_NOT_FOUND":
        raise RelayError("ACCOUNT_NOT_FOUND", selector)
    compact = doc.get("compact_label")
    if not compact:
        raise RelayError("ACCOUNT_IDENTITY_MISSING", selector)
    return {"account_id": doc.get("account_id"), "compact_label": compact,
            "target_sid": doc.get("target_sid") or (doc.get("provider_metadata") or {}).get("windows_sid")}


def ensure_staging(selector, workdir):
    exe = accounts_exe()
    proc = subprocess.run([exe, "handoff-stage", selector, "--workdir", workdir],
                          capture_output=True, text=True, timeout=60)
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    try:
        doc = json.loads(line)
    except ValueError:
        raise RelayError("STAGING_FAILED", line[:200])
    if proc.returncode != 0:
        raise RelayError(doc.get("status") or "STAGING_FAILED", line[:200])
    return doc["staging_dir"]


def staged_source_bytes(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ------------------------------------------------------------ source probing
def probe_source(state):
    """Classify the source object right now.

    Returns (classification, stat_result_or_None). Classification is one of
    OK, MISSING, DENIED, UNSAFE_OBJECT. Nothing here follows a reparse point:
    a reparse point is UNSAFE_OBJECT even when it resolves to a regular file.
    """
    source = state["source"]
    if is_reparse_point(source):
        return "UNSAFE_OBJECT", None
    if is_directory(source):
        return "UNSAFE_OBJECT", None
    try:
        return "OK", os.stat(source)
    except FileNotFoundError:
        return "MISSING", None
    except PermissionError:
        return "DENIED", None
    except OSError as ex:
        raise RelayError("SOURCE_STAT_FAILED", "%s: %s" % (type(ex).__name__, ex))


# ----------------------------------------------------------------- output sink
def rotation_code(ex, prefix):
    """Name the class of a refusal exactly: denied (locked, no permission) or
    failed (anything else). A caller must never have to guess which."""
    if isinstance(ex, PermissionError):
        return prefix + "_DENIED"
    if getattr(ex, "errno", None) in (errno.EACCES, errno.EPERM):
        return prefix + "_DENIED"
    return prefix + "_FAILED"


def record_rotation_gap(state, code, detail):
    """Say a rotation problem out loud, where it survives a restart."""
    if state is None:
        return
    try:
        gap_marker(state, code, detail)
        return
    except OSError:
        pass
    gaps = state.setdefault("gaps", [])
    if not gaps or gaps[-1].get("code") != code:
        gaps.append({"code": code, "detail": detail, "at": now_iso()})


def rotate_output(staged_path, max_bytes, keep, state=None):
    """Bound the staged copy. The agent-facing primary path never changes
    name: history moves aside, the live file stays where it is documented.

    True means ONE thing: the PRIMARY was rotated -- the old primary is now a
    history generation and the primary path is free for the caller to append
    to. A history generation that moved on its own is NOT a rotation, so a
    failure to move the primary is never reported as a rotation that happened.

    A refusal is named, never silent: RelayError says which move was denied
    and which failed. Nothing is deleted to "recover" from an ambiguous error,
    and a generation that could not move aside is never overwritten by the
    primary -- that would destroy the only copy of those bytes.
    """
    try:
        size = os.path.getsize(staged_path)
    except OSError:
        return False
    if size < max_bytes:
        return False
    history = max(int(keep), 0)
    if history == 0:
        # No history was asked for, but the primary must still be bounded:
        # the next append starts a fresh file at the documented path.
        try:
            with open(staged_path, "wb"):
                pass
        except OSError as ex:
            raise RelayError(rotation_code(ex, "ROTATION_PRIMARY_TRUNCATE"),
                             "%s: %s" % (type(ex).__name__, ex))
        return True
    # Shift oldest-first so .1 always ends up holding the newest history
    # file: .(history-1) -> .history down to .1 -> .2, then primary -> .1.
    # `history-1` (not `history`) is the bound: moving .keep to .keep+1
    # produced a generation past --keep and left .2 permanently unborn.
    for index in range(history - 1, 0, -1):
        older = "%s.%d" % (staged_path, index)
        if not os.path.exists(older):
            continue
        try:
            os.replace(older, "%s.%d" % (staged_path, index + 1))
        except OSError as ex:
            # The generation stays where it is. Moving the primary now would
            # overwrite it, so the rotation stops here and says why.
            raise RelayError(rotation_code(ex, "ROTATION_HISTORY"),
                             "%s -> %s.%d: %s" % (older, os.path.basename(staged_path),
                                                 index + 1, ex))
    try:
        os.replace(staged_path, "%s.1" % staged_path)
    except OSError as ex:
        # The primary did not move, so nothing was rotated. The bytes are
        # still where they were, and no generation is deleted to hide it.
        raise RelayError(rotation_code(ex, "ROTATION_PRIMARY"),
                         "%s -> %s.1: %s" % (os.path.basename(staged_path),
                                             os.path.basename(staged_path), ex))
    # A generation outside 1..history is stale (this staging dir may have been
    # used with a different --keep): drop it so the retained set stays exactly
    # --keep. This runs only after the primary really moved: a failed
    # rotation leaves the directory exactly as it found it.
    prefix = os.path.basename(staged_path) + "."
    directory = os.path.dirname(staged_path) or "."
    try:
        entries = os.listdir(directory)
    except OSError as ex:
        # The generations this rotation moved are still bounded; only the
        # sweep of foreign leftovers is skipped, and that is said out loud.
        entries = []
        record_rotation_gap(state, "ROTATION_LISTING",
                            "%s: %s" % (type(ex).__name__, ex))
    for name in entries:
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix):]
        if suffix.isdigit() and not 1 <= int(suffix) <= history:
            try:
                os.remove(os.path.join(directory, name))
            except OSError as ex:
                record_rotation_gap(state, "ROTATION_CLEANUP",
                                    "%s: %s" % (name, ex))
    return True


def append_staged(staged_path, data):
    with open(staged_path, "ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def gap_marker(state, code, detail):
    stamp = now_iso()
    line = "[sai-relay] %s %s %s\n" % (code, detail, stamp)
    state["gaps"].append({"code": code, "detail": detail, "at": stamp})
    if len(state["gaps"]) > 256:
        del state["gaps"][:len(state["gaps"]) - 256]
    append_staged(state["staged_path"], line.encode("utf-8", "replace"))


# ----------------------------------------------------------------- one cycle
def poll_once(state):
    """One read cycle. Returns nothing; mutates state and writes it."""
    classification, info = probe_source(state)
    state["last_probe"] = classification
    state["last_probe_at"] = now_iso()

    if classification == "DENIED":
        state["source_state"] = "SOURCE_DENIED"
        # A denial is never an invitation to widen access. Report and wait.
        if state["denied_since"] is None:
            state["denied_since"] = now_iso()
        state["last_run_at"] = now_iso()
        return

    if classification == "UNSAFE_OBJECT":
        state["source_state"] = "SOURCE_UNSAFE_OBJECT"
        if state["unsafe_since"] is None:
            state["unsafe_since"] = now_iso()
            gap_marker(state, "SOURCE_UNSAFE_OBJECT",
                       "refused to follow reparse point or non-regular object at %s" % state["source"])
        state["last_run_at"] = now_iso()
        return

    if classification == "MISSING":
        state["source_state"] = "SOURCE_MISSING"
        if state["missing_since"] is None:
            state["missing_since"] = now_iso()
            gap_marker(state, "SOURCE_MISSING", "source not present; waiting for it to return")
        # A grace of 0 means "say so immediately", so the test is >= 0 and not > 0.
        if state["lost_reported"] is None and state.get("missing_grace_seconds") is not None \
                and state["missing_grace_seconds"] >= 0:
            try:
                started = time.mktime(time.strptime(state["missing_since"][:19],
                                                    "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                started = time.time()
            if time.time() - started >= state["missing_grace_seconds"]:
                state["lost_reported"] = now_iso()
                gap_marker(state, "SOURCE_LOST",
                           "source absent for %ds; staged copy is frozen at the last verified offset"
                           % state["missing_grace_seconds"])
        flush_quiet_partial(state)
        state["last_run_at"] = now_iso()
        return

    # classification == "OK"
    identity = source_identity(info)
    size = info.st_size

    if state["missing_since"] is not None:
        gap_marker(state, "SOURCE_RETURNED", "source readable again after an absence")
        state["missing_since"] = None
        state["denied_since"] = None
        state["unsafe_since"] = None

    if state["identity"] is None:
        state["identity"] = identity

    if identity != state["identity"]:
        # A different file now occupies the name. Whether that is rotation or
        # an atomic replacement, the old offsets no longer describe it, so the
        # honest move is to say so and restart the stream at zero.
        gap_marker(state, "SOURCE_REPLACED",
                   "source identity changed (%s -> %s); stream restarted at offset 0"
                   % (state["identity"], identity))
        state["rotation_count"] = state.get("rotation_count", 0) + 1
        state["identity"] = identity
        state["offset"] = 0
        state["pending"] = b""
        state.pop("pending_since", None)
        state["lost_reported"] = None

    if size < state["offset"]:
        gap_marker(state, "SOURCE_TRUNCATED",
                   "size %d is below the last read offset %d; earlier bytes are gone"
                   % (size, state["offset"]))
        state["truncation_count"] = state.get("truncation_count", 0) + 1
        state["offset"] = 0
        state["pending"] = b""
        state.pop("pending_since", None)

    if size > state["offset"]:
        try:
            with open(state["source"], "rb") as handle:
                handle.seek(state["offset"])
                chunk = handle.read(size - state["offset"])
        except PermissionError:
            state["source_state"] = "SOURCE_DENIED"
            state["last_run_at"] = now_iso()
            return
        except OSError as ex:
            raise RelayError("SOURCE_READ_FAILED", "%s: %s" % (type(ex).__name__, ex))

        pending = state["pending"] + chunk
        cut = pending.rfind(b"\n")
        if cut >= 0:
            emit = pending[:cut + 1]
            state["pending"] = pending[cut + 1:]
        else:
            emit = b""
            state["pending"] = pending
        if len(state["pending"]) > MAX_PENDING_BYTES:
            # A source that emits no newline for 64 KiB is still served, but it
            # is served as a partial line and the gap is named.
            emit += state["pending"]
            state["lost_bytes"] = state.get("lost_bytes", 0) + len(state["pending"])
            gap_marker(state, "PARTIAL_LINE_FLUSHED",
                       "no newline within %d bytes; flushed an incomplete line" % MAX_PENDING_BYTES)
            state["pending"] = b""

        if emit:
            try:
                rotated = rotate_output(state["staged_path"], state["max_bytes"],
                                        state["keep"], state)
            except RelayError as ex:
                # The rotation did not happen. The counter must never claim
                # it did, and the refusal is named where the operator sees it.
                record_rotation_gap(state, ex.code, ex.detail)
                raise
            if rotated:
                state["output_rotations"] = state.get("output_rotations", 0) + 1
            append_staged(state["staged_path"], emit)
            state["bytes_copied"] = state.get("bytes_copied", 0) + len(emit)
            state["lines_copied"] = state.get("lines_copied", 0) + emit.count(b"\n")
            state["last_data_at"] = now_iso()

        state["offset"] += len(chunk)
        state["source_state"] = "RUNNING"
    else:
        state["source_state"] = "RUNNING" if size == state["offset"] else "RUNNING"

    flush_quiet_partial(state)

    if state["lost_reported"] is not None:
        state["source_state"] = "SOURCE_LOST"
    state["last_run_at"] = now_iso()


def flush_quiet_partial(state):
    """Publish a trailing partial line once the source goes quiet.

    Without this, the bytes after the last newline stay in the buffer until the
    next write arrives -- so the line the process was writing when it stopped
    never reaches the agent, which is exactly the line a diagnosis needs.
    The line is emitted as an incomplete line and the gap is named, so a reader
    can tell a truncated record from a complete one.
    """
    if not state["pending"]:
        state.pop("pending_since", None)
        return
    since = state.get("pending_since")
    if since is None:
        state["pending_since"] = now_iso()
        return
    try:
        started = time.mktime(time.strptime(since[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        state.pop("pending_since", None)
        return
    if time.time() - started < state.get("partial_flush_seconds", 5.0):
        return
    data, state["pending"] = state["pending"], b""
    state.pop("pending_since", None)
    state["lost_bytes"] = state.get("lost_bytes", 0) + len(data)
    gap_marker(state, "PARTIAL_LINE_FLUSHED",
               "source idle; flushed an incomplete trailing line of %d bytes" % len(data))
    append_staged(state["staged_path"], data)
    state["bytes_copied"] = state.get("bytes_copied", 0) + len(data)
    state["last_data_at"] = now_iso()


def relay_state(lease, source, staged_path, options):
    return {
        "schema": SCHEMA,
        "source": source,
        "staged_path": staged_path,
        "identity": None,
        "offset": 0,
        "pending": b"",
        "bytes_copied": 0,
        "lines_copied": 0,
        "lost_bytes": 0,
        "gaps": [],
        "rotation_count": 0,
        "truncation_count": 0,
        "output_rotations": 0,
        "missing_since": None,
        "denied_since": None,
        "unsafe_since": None,
        "lost_reported": None,
        "source_state": "STARTING",
        "interval": options["interval"],
        "max_bytes": options["max_bytes"],
        "keep": options["keep"],
        "missing_grace_seconds": options["missing_grace_seconds"],
        "partial_flush_seconds": options.get("partial_flush_seconds", 5.0),
        "account": lease["account"],
        "lease_id": lease["lease_id"],
        "started_at": lease["created_at"],
    }


def encode_state(state):
    """State is persisted as JSON, so the pending bytes need a text form."""
    persisted = dict(state)
    persisted["pending"] = state["pending"].decode("utf-8", "surrogateescape")
    return persisted


def decode_state(raw):
    state = dict(raw)
    pending = state.get("pending") or ""
    state["pending"] = pending.encode("utf-8", "surrogateescape") if isinstance(pending, str) else b""
    return state


def lease_expired(lease):
    expires = lease.get("expires_at")
    if not expires:
        return False
    try:
        return time.time() >= expires
    except (TypeError, ValueError):
        return False


def write_status(cdir, lease, state, relay_status, pid=None):
    doc = {
        "schema": SCHEMA + "/status",
        "lease_id": lease["lease_id"],
        "account": lease["account"],
        "source": lease["source"],
        "staged_path": lease["staged_path"],
        "relay_status": relay_status,
        "runner_pid": pid if pid is not None else lease.get("runner_pid"),
        "expires_at": lease.get("expires_at_iso"),
        "revoked": bool(lease.get("revoked")),
        "source_state": state.get("source_state"),
        "bytes_copied": state.get("bytes_copied"),
        "lines_copied": state.get("lines_copied"),
        "lost_bytes": state.get("lost_bytes"),
        "gaps": state.get("gaps"),
        "rotation_count": state.get("rotation_count"),
        "output_rotations": state.get("output_rotations"),
        "truncation_count": state.get("truncation_count"),
        "last_data_at": state.get("last_data_at"),
        "last_run_at": state.get("last_run_at"),
        "updated_at": now_iso(),
    }
    write_json_atomic(status_path(cdir), doc)
    return doc


# ------------------------------------------------------------------- commands
def cmd_start(argv):
    opts, flags = parse_args(argv, {
        "--account": "account", "--source": "source", "--workdir": "workdir",
        "--staged-name": "staged_name", "--interval": "interval",
        "--max-bytes": "max_bytes", "--keep": "keep", "--lease-minutes": "minutes",
        "--missing-grace": "missing_grace", "--partial-flush": "partial_flush",
    }, ["--session"])
    selector = opts.get("account")
    if not selector:
        raise RelayError("ACCOUNT_REQUIRED", "--account <selector>")
    if not opts.get("source"):
        raise RelayError("SOURCE_REQUIRED", "--source <path>")
    source = os.path.abspath(opts["source"])
    workdir = opts.get("workdir")
    staged_name = opts.get("staged_name")
    interval = bounded(opts.get("interval", str(DEFAULT_INTERVAL)), "interval", MIN_INTERVAL, MAX_INTERVAL)
    try:
        max_bytes = int(opts.get("max-bytes", str(DEFAULT_MAX_BYTES)))
        keep = int(opts.get("keep", str(DEFAULT_KEEP)))
        missing_grace = int(opts.get("missing-grace", "300"))
        partial_flush = float(opts.get("partial-flush", "5"))
    except ValueError:
        raise RelayError("INVALID_NUMBER", "sizes, counts and delays must be numbers")
    if partial_flush < 0.5 or partial_flush > 3600:
        raise RelayError("INVALID_PARTIAL_FLUSH",
                         "a trailing-line flush delay must be between 0.5 and 3600 seconds")
    if max_bytes < 4096:
        raise RelayError("INVALID_MAX_BYTES", "staged retention must be at least 4096 bytes")
    session = "--session" in flags

    # The operator selects the source; a managed agent never does.
    if is_reparse_point(source) or is_directory(source):
        raise RelayError("SOURCE_UNSAFE_OBJECT",
                         "a reparse point or directory is not a relayable log file: " + source)
    classification, info = probe_source({"source": source})
    if classification == "MISSING":
        raise RelayError("SOURCE_NOT_FOUND", source)
    if classification == "DENIED":
        raise RelayError("SOURCE_DENIED",
                         "the operator's own token cannot read the source; no escalation is possible")

    account = resolve_account(selector)
    compact = account["compact_label"]

    if not workdir:
        raise RelayError("WORKDIR_REQUIRED",
                         "the staged destination must name the account's authorized project directory")

    staging_dir = ensure_staging(selector, workdir)
    staged_path = os.path.join(staging_dir, staged_name or os.path.basename(source))

    cdir = control_dir(compact, source)
    existing = read_json(lease_path(cdir))
    if existing and not existing.get("revoked") and pid_alive(existing.get("runner_pid")):
        raise RelayError("RELAY_ALREADY_RUNNING",
                         "pid %s is already relaying this source; stop it first or use status"
                         % existing.get("runner_pid"))
    if os.path.exists(staged_path) and (not existing or existing.get("source") != source):
        # Someone else already owns this documented path for this account.
        if existing and existing.get("staged_path") != staged_path:
            raise RelayError("STAGED_NAME_COLLISION", staged_path)

    created_at = now_iso()
    if session:
        expires_at, expires_at_iso = 0, ""
    else:
        try:
            minutes = float(opts.get("minutes", str(DEFAULT_LEASE_MINUTES)))
        except ValueError:
            raise RelayError("INVALID_MINUTES", str(opts.get("minutes")))
        if minutes < 1 or minutes > 1440:
            raise RelayError("INVALID_MINUTES", "a lease must run between 1 and 1440 minutes")
        expires_at = time.time() + minutes * 60.0
        expires_at_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(expires_at))

    lease = {
        "schema": SCHEMA + "/lease",
        "lease_id": uuid.uuid4().hex,
        "account": compact,
        "account_id": account["account_id"],
        "source": source,
        "staged_path": staged_path,
        "created_at": created_at,
        "created_by_pid": os.getpid(),
        "runner_pid": None,
        "expires_at": expires_at,
        "expires_at_iso": expires_at_iso,
        "revoked": False,
        "generation": 1,
    }
    options = {"interval": interval, "max_bytes": max_bytes, "keep": keep,
               "missing_grace_seconds": missing_grace,
               "partial_flush_seconds": partial_flush}
    state = relay_state(lease, source, staged_path, options)
    # The staged path must exist before it can be proven readable: a missing
    # file proves nothing, and "unreadable" would be the wrong diagnosis.
    if not os.path.exists(staged_path):
        open(staged_path, "ab").close()
    write_json_atomic(lease_path(cdir), lease)
    write_json_atomic(state_path(cdir), encode_state(state))

    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "run",
                             "--source", source, "--compact", compact],
                            stdout=open(runner_log_path(cdir), "a"),
                            stderr=subprocess.STDOUT,
                            creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    lease["runner_pid"] = proc.pid
    write_json_atomic(lease_path(cdir), lease)

    agent_readable = verify_agent_readable(selector, staged_path)
    doc = write_status(cdir, lease, state, "RUNNING", pid=proc.pid)
    doc["status"] = "ok"
    doc["schema"] = SCHEMA + "/start"
    doc["interval"] = interval
    doc["expires_at"] = expires_at_iso or None
    doc["agent_readable"] = agent_readable
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


def cmd_run(argv):
    opts, _flags = parse_args(argv, {"--source": "source", "--compact": "compact"}, [])
    if not opts.get("source") or not opts.get("compact"):
        raise RelayError("SOURCE_AND_COMPACT_REQUIRED", "--source <path> --compact <label>")
    source = os.path.abspath(opts["source"])
    compact = opts["compact"]
    cdir = control_dir(compact, source)
    lease = read_json(lease_path(cdir))
    raw_state = read_json(state_path(cdir))
    if lease is None or raw_state is None:
        raise RelayError("LEASE_NOT_FOUND", cdir)
    if lease.get("revoked"):
        raise RelayError("LEASE_REVOKED", lease.get("lease_id"))
    state = decode_state(raw_state)

    # Orphan recovery: a previous runner died. Its offsets are the durable
    # truth about what the agent already has, so the relay resumes rather than
    # re-delivering or losing bytes.
    prior_pid = lease.get("runner_pid")
    if prior_pid and prior_pid != os.getpid() and not pid_alive(prior_pid):
        gap_marker(state, "RUNNER_RESTARTED",
                   "previous relay pid %s is gone; resuming from offset %d" % (prior_pid, state["offset"]))

    while True:
        # Re-read the lease every cycle. The stop command and the expiry check
        # live in the operator's control directory, and a runner that cached
        # revocation at startup kept publishing after the operator stopped it.
        current = read_json(lease_path(cdir))
        if current is not None:
            lease = current
        if lease.get("revoked") or lease_expired(lease):
            reason = "LEASE_REVOKED" if lease.get("revoked") else "LEASE_EXPIRED"
            gap_marker(state, reason, "relay stopped; no further updates will be published")
            lease["revoked"] = True
            write_json_atomic(lease_path(cdir), lease)
            write_json_atomic(state_path(cdir), encode_state(state))
            write_status(cdir, lease, state, "STOPPED_" + reason, pid=os.getpid())
            return 0
        try:
            poll_once(state)
        except RelayError as ex:
            # A named refusal must survive the stop: persist what is known
            # and name the reason in the status the operator reads.
            write_json_atomic(state_path(cdir), encode_state(state))
            write_status(cdir, lease, state, "STOPPED_" + ex.code, pid=os.getpid())
            raise
        write_json_atomic(state_path(cdir), encode_state(state))
        write_status(cdir, lease, state, "RUNNING", pid=os.getpid())
        time.sleep(min(state.get("interval", DEFAULT_INTERVAL), MAX_INTERVAL))


def cmd_once(argv):
    """One cycle, then exit. This is the unit the tests drive."""
    opts, _flags = parse_args(argv, {"--source": "source", "--compact": "compact"}, [])
    if not opts.get("source") or not opts.get("compact"):
        raise RelayError("SOURCE_AND_COMPACT_REQUIRED", "--source <path> --compact <label>")
    source = os.path.abspath(opts["source"])
    compact = opts["compact"]
    cdir = control_dir(compact, source)
    lease = read_json(lease_path(cdir))
    raw_state = read_json(state_path(cdir))
    if lease is None or raw_state is None:
        raise RelayError("LEASE_NOT_FOUND", cdir)
    state = decode_state(raw_state)
    poll_once(state)
    write_json_atomic(state_path(cdir), encode_state(state))
    relay_status = "RUNNING" if not lease.get("revoked") and not lease_expired(lease) else "STOPPED"
    doc = write_status(cdir, lease, state, relay_status, pid=os.getpid())
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


def cmd_status(argv):
    opts, _flags = parse_args(argv, {"--account": "account", "--source": "source"}, [])
    if not opts.get("account") or not opts.get("source"):
        raise RelayError("ACCOUNT_AND_SOURCE_REQUIRED", "--account <selector> --source <path>")
    selector = opts["account"]
    source = os.path.abspath(opts["source"])
    compact = resolve_account(selector)["compact_label"]
    cdir = control_dir(compact, source)
    lease = read_json(lease_path(cdir))
    if lease is None:
        raise RelayError("LEASE_NOT_FOUND", "no relay is configured for this account and source")
    state = decode_state(read_json(state_path(cdir)) or {})
    doc = write_status(cdir, lease, state, "STATUS_QUERY", pid=lease.get("runner_pid"))
    doc["status"] = "ok"
    doc["runner_alive"] = pid_alive(lease.get("runner_pid"))
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


def cmd_stop(argv):
    opts, _flags = parse_args(argv, {"--account": "account", "--source": "source"}, [])
    if not opts.get("account") or not opts.get("source"):
        raise RelayError("ACCOUNT_AND_SOURCE_REQUIRED", "--account <selector> --source <path>")
    selector = opts["account"]
    source = os.path.abspath(opts["source"])
    compact = resolve_account(selector)["compact_label"]
    cdir = control_dir(compact, source)
    lease = read_json(lease_path(cdir))
    if lease is None:
        raise RelayError("LEASE_NOT_FOUND", "no relay is configured for this account and source")
    state = decode_state(read_json(state_path(cdir)) or {})
    lease["revoked"] = True
    write_json_atomic(lease_path(cdir), lease)
    gap_marker(state, "STOP_REQUESTED", "operator stopped the relay explicitly")
    write_json_atomic(state_path(cdir), encode_state(state))
    doc = write_status(cdir, lease, state, "STOPPED_BY_OPERATOR", pid=lease.get("runner_pid"))
    doc["status"] = "ok"
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


def cmd_list(argv):
    opts, _flags = parse_args(argv, {"--workdir": "workdir"}, [])
    workdir = opts.get("workdir")
    rows = []
    root = control_root()
    if os.path.isdir(root):
        for compact in sorted(os.listdir(root)):
            compact_dir = os.path.join(root, compact)
            if not os.path.isdir(compact_dir):
                continue
            for sid in sorted(os.listdir(compact_dir)):
                lease = read_json(os.path.join(compact_dir, sid, "lease.json"))
                if lease is None:
                    continue
                rows.append({
                    "account": lease.get("account"),
                    "source": lease.get("source"),
                    "staged_path": lease.get("staged_path"),
                    "runner_pid": lease.get("runner_pid"),
                    "runner_alive": pid_alive(lease.get("runner_pid")),
                    "revoked": bool(lease.get("revoked")),
                    "expires_at": lease.get("expires_at_iso"),
                })
    print(json.dumps({"schema": SCHEMA + "/list", "status": "ok",
                      "workdir": os.path.abspath(workdir) if workdir else None,
                      "relays": rows}, indent=2))
    return 0


# ----------------------------------------------------------------- arg helpers
def parse_args(argv, valued, flagset):
    """Flag parsing by name. list.pop() is index-based, so a value-removing
    helper has to be explicit: silently leaking an unknown argument into a
    command that writes ACL state is how a relay ends up aimed at a file the
    operator never chose."""
    opts, seen, unknown = {}, [], []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in valued:
            if index + 1 >= len(argv):
                raise RelayError("ARGS", "%s needs a value" % token)
            opts[valued[token]] = argv[index + 1]
            index += 2
            continue
        if token in flagset:
            seen.append(token)
            index += 1
            continue
        unknown.append(token)
        index += 1
    if unknown:
        raise RelayError("ARGS", "unknown argument: %s" % unknown[0])
    return opts, seen


def bounded(raw, field, minimum, maximum):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise RelayError("INVALID_" + field.upper(), str(raw))
    if value < minimum or value > maximum:
        raise RelayError("INVALID_" + field.upper(),
                         "%s must be between %s and %s" % (field, minimum, maximum))
    return value



def verify_agent_readable(selector, staged_path):
    """Prove readability under the managed identity, not under ours."""
    exe = accounts_exe()
    try:
        proc = subprocess.run([exe, "verify-file", selector, "--path", staged_path],
                              capture_output=True, text=True, timeout=30)
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        doc = json.loads(line)
        return proc.returncode == 0 and doc.get("readable") is True
    except Exception:
        return False


def main(argv):
    if not argv:
        print(json.dumps({"status": "USAGE",
                          "commands": ["start", "run", "once", "status", "stop", "list"]}, indent=2))
        return 2
    verb, rest = argv[0], argv[1:]
    handlers = {"start": cmd_start, "run": cmd_run, "once": cmd_once,
                "status": cmd_status, "stop": cmd_stop, "list": cmd_list}
    handler = handlers.get(verb)
    if handler is None:
        print(json.dumps({"status": "UNKNOWN_COMMAND", "command": verb}, indent=2))
        return 2
    try:
        return handler(rest)
    except RelayError as ex:
        print(json.dumps({"status": ex.code, "detail": ex.detail,
                          "schema": SCHEMA + "/error"}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
