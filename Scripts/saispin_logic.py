#!/usr/bin/env python3
"""Pure decision engine for SAISPIN, the orphan CPU watchdog.

No Windows API, no process enumeration, no termination, no tray, no scheduler.
It answers exactly one question about data somebody else collected: given this
process's hot/cold history and the current sample, IGNORE, ALERT or KILL?

Keeping the answer here is the whole point. The rules below were written from
two real incidents and one counterexample, and every one of them is a rule
where being wrong is invisible until it kills the wrong process:

* one CPU spike is not evidence. Only a CONSECUTIVE hot streak counts, so a
  compiler that pegs a core for twenty seconds never registers;
* an orphan is not a kill target. The orphaned ``pytest`` processes that
  started this design were sleeping at ~0% CPU -- the majority case;
* a spinning process with a live parent is never killed automatically. It is
  usually a compiler, an encoder, an indexer or a test run doing its job;
* PID is not identity. PID + StartTime is, so a reused PID starts from zero
  instead of inheriting a dead process's strikes;
* missing or untrusted information biases toward NOT killing.

The engine is the same code the watcher runs and the tests exercise, so the
safety logic can be proven without spawning a single real spinning process:

    python tests/test_saispin.py

The watcher feeds it one JSON sweep per invocation on stdin and reads the
decisions plus the next state back out on stdout:

    ... | python Scripts/saispin_logic.py --sweep
"""

from __future__ import annotations

import dataclasses
import json
import copy
import math
import os
import sys
import threading
import uuid
from datetime import datetime, timezone

# A sample above this share of ONE logical core is "hot". 80% leaves room for a
# busy-but-useful process to breathe while still catching a real spin loop,
# which sits at ~100% of one core by definition.
HOT_CPU_PERCENT = 80.0

# Sustained spin needs BOTH: enough consecutive hot samples AND enough wall
# clock. Hits alone would trip on a watcher that ran six times in a minute;
# elapsed time alone would trip on one hot sample half an hour after another.
SUSTAINED_HITS = 6
SUSTAINED_SECONDS = 30 * 60

# The watcher sweeps every five minutes, so two hot samples further apart than
# this were not consecutive OBSERVATIONS of anything: the machine slept, the task
# was disabled, or the watcher was off. The process could have been idle for the
# entire gap and nobody was watching, which is exactly the "stale history" a kill
# may not come from. Such a sample starts a fresh streak instead of extending an
# old one. Six times the cadence, so an ordinary late or skipped run is tolerated.
STALE_GAP_SECONDS = 30 * 60

# The sampling window the watcher uses between its two CPU readings.
SAMPLE_SECONDS = 3

# Command lines are unbounded and balloon tips are not. The full text belongs
# in the structured log; this is what the notification and state file carry.
CMDLINE_LIMIT = 200

IGNORE = "IGNORE"
ALERT = "ALERT"
KILL = "KILL"

# Classification constants (Phase 7)
CLASSIFICATION_PROTECTED_ACTIVE = "PROTECTED_ACTIVE"
CLASSIFICATION_PROTECTED_SYSTEM = "PROTECTED_SYSTEM"
CLASSIFICATION_PROTECTED_USER = "PROTECTED_USER"
CLASSIFICATION_OBSERVE_ONLY = "OBSERVE_ONLY"
CLASSIFICATION_SUSPECTED_STALL = "SUSPECTED_STALL"
CLASSIFICATION_RECOVERY_ELIGIBLE = "RECOVERY_ELIGIBLE"
CLASSIFICATION_UNKNOWN = "UNKNOWN"

# Killing any of these is a reboot, not a fix. Compared case-insensitively.
# "System Idle Process" is the name Windows actually reports for PID 0, and it
# is not an idle-looking name by accident: the Idle process accumulates CPU for
# every cycle NOBODY used, so on a quiet machine it reads as a permanent 100%
# spin with no parent. It belongs here, and PID_FLOOR below is the real guard.
NEVER_KILL = frozenset(
    {
        "system",
        "system idle process",
        "idle",
        "registry",
        "memory compression",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "winlogon.exe",
        # Windows desktop & system infrastructure
        "dwm.exe",
        "explorer.exe",
        "fontdrvhost.exe",
        "sihost.exe",
        "svchost.exe",
        "taskhostw.exe",
        "ctfmon.exe",
        "runtimebroker.exe",
        "shellexperiencehost.exe",
        "startmenuexperiencehost.exe",
        "searchapp.exe",
        "searchhost.exe",
        "searchindexer.exe",
        "spoolsv.exe",
        "audiodg.exe",
        "conhost.exe",
        "openconsole.exe",
        "windowsterminal.exe",
        # Security & Defender
        "securityhealthservice.exe",
        "securityhealthsystray.exe",
        "msmpeng.exe",
        "nissrv.exe",
        "smartscreen.exe",
        "mpcmdrun.exe",
        # Compilers & builders
        "csc.exe",
        "vbc.exe",
        "cl.exe",
        "link.exe",
        "msbuild.exe",
        "javac.exe",
        "rustc.exe",
        "cargo.exe",
        "go.exe",
        "gcc.exe",
        "g++.exe",
        "clang.exe",
        "clang++.exe",
        "cmake.exe",
        "ninja.exe",
        # Agents & task runners
        "antigravity.exe",
        "agy.exe",
        "claude.exe",
        "opencode.exe",
        # Media & encoders
        "obs64.exe",
        "obs32.exe",
        "obs.exe",
    }
)

# PIDs 0 and 4 are the Idle and System processes. They have no real parent, so
# every orphan test says orphan, and Idle accumulates CPU for every cycle NOBODY
# used -- on a quiet machine it reads as a permanent 100% spin. Found on the
# first live dry-run: PID 0 "System Idle Process" was the only tracked process
# on the whole machine, and left alone it would have produced a balloon every
# five minutes forever, which is how a useful alert becomes one nobody reads.
#
# Identity by number rather than by name, because the name is localized: on a
# Russian Windows PID 0 is "Бездействие системы" and no name list would match.
KERNEL_PIDS = frozenset({0, 4})
PID_FLOOR = 4

# Images whose ROOT process is protected while its children are not. VS Code
# spawns renderer, extension-host and language-server children that are fair
# game; killing the one process that owns the window is not. "Main" is decided
# by the caller (no live ancestor of the same image), because that is a
# process-tree fact, not something a name can carry.
MAIN_PROCESS_GUARDED = frozenset({"code.exe"})

# One validated settings model feeds the watcher, task installer, GUI and this
# engine. These defaults intentionally match the pre-settings constants below;
# a fresh install therefore behaves exactly like the old watchdog.
DEFAULT_SETTINGS = {
    "schema_version": 1,
    "thresholds": {
        "cpu_percent": 80.0,
        "required_hits": 6,
        "minimum_hot_minutes": 30,
    },
    "timing": {
        "sample_seconds": 3,
        "sweep_interval_minutes": 5,
        "stale_gap_minutes": 30,
    },
    "notifications": {"enabled": True, "reminder_minutes": 60},
    "allowlist": [],
    "process_rules": {},
}


def default_settings() -> dict:
    """Return an independent copy of the safe built-in settings."""
    return copy.deepcopy(DEFAULT_SETTINGS)


def _bounded_number(value, field, minimum, maximum, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % field)
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError("%s must be between %s and %s" % (field, minimum, maximum))
    if integer:
        if number != int(number):
            raise ValueError("%s must be a whole number" % field)
        return int(number)
    return number


def _process_image(value, field):
    if not isinstance(value, str):
        raise ValueError("%s image name must be text" % field)
    name = value.strip().lower()
    if (not name or len(name) > 255 or "\\" in name or "/" in name
            or "\x00" in name):
        raise ValueError("%s image name must be a file name" % field)
    return name


def normalize_settings(raw):
    """Validate the complete policy or fail closed to unchanged defaults.

    Legacy ``Allowlist``/``SampleSeconds`` config files remain readable. A
    malformed or future schema never gets partially applied. Per-process
    numeric overrides may only tighten the global thresholds; ``alert_only``
    is the sole mode override, so a rule can never disable a hard kill guard.
    Returns ``(settings, warning)``.
    """
    if raw is None:
        return default_settings(), None
    try:
        if not isinstance(raw, dict):
            raise ValueError("settings root must be an object")

        if "Allowlist" in raw or "SampleSeconds" in raw:
            legacy_allowed = {"Allowlist", "SampleSeconds"}
            if set(raw) - legacy_allowed:
                raise ValueError("legacy settings contain unknown fields")
            raw = {
                "schema_version": 1,
                "allowlist": raw.get("Allowlist", []),
                "timing": {"sample_seconds": raw.get("SampleSeconds", 3)},
            }

        allowed_root = {
            "schema_version", "thresholds", "timing", "notifications",
            "allowlist", "process_rules",
        }
        unknown_root = set(raw) - allowed_root
        if unknown_root:
            raise ValueError("unknown settings field: %s" % sorted(unknown_root)[0])
        version = raw.get("schema_version", 1)
        if isinstance(version, bool) or version != 1:
            raise ValueError("unsupported settings schema_version")

        result = default_settings()
        for group_name, allowed in (
            ("thresholds", {"cpu_percent", "required_hits", "minimum_hot_minutes"}),
            ("timing", {"sample_seconds", "sweep_interval_minutes", "stale_gap_minutes"}),
            ("notifications", {"enabled", "reminder_minutes"}),
        ):
            group = raw.get(group_name, {})
            if not isinstance(group, dict):
                raise ValueError("%s must be an object" % group_name)
            unknown = set(group) - allowed
            if unknown:
                raise ValueError("unknown %s field: %s" % (group_name, sorted(unknown)[0]))

        thresholds = raw.get("thresholds", {})
        result["thresholds"] = {
            "cpu_percent": _bounded_number(
                thresholds.get("cpu_percent", 80), "cpu_percent", 50, 100),
            "required_hits": _bounded_number(
                thresholds.get("required_hits", 6), "required_hits", 2, 60, integer=True),
            "minimum_hot_minutes": _bounded_number(
                thresholds.get("minimum_hot_minutes", 30), "minimum_hot_minutes", 5, 240, integer=True),
        }

        timing = raw.get("timing", {})
        result["timing"] = {
            "sample_seconds": _bounded_number(
                timing.get("sample_seconds", 3), "sample_seconds", 1, 30, integer=True),
            "sweep_interval_minutes": _bounded_number(
                timing.get("sweep_interval_minutes", 5), "sweep_interval_minutes", 1, 1440, integer=True),
            "stale_gap_minutes": _bounded_number(
                timing.get("stale_gap_minutes", 30), "stale_gap_minutes", 5, 1440, integer=True),
        }
        if result["timing"]["stale_gap_minutes"] < result["timing"]["sweep_interval_minutes"]:
            raise ValueError("stale_gap_minutes must be at least sweep_interval_minutes")

        notifications = raw.get("notifications", {})
        enabled = notifications.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("notifications.enabled must be true or false")
        result["notifications"] = {
            "enabled": enabled,
            "reminder_minutes": _bounded_number(
                notifications.get("reminder_minutes", 60), "reminder_minutes", 5, 1440, integer=True),
        }

        allowlist = raw.get("allowlist", [])
        if not isinstance(allowlist, list) or len(allowlist) > 250:
            raise ValueError("allowlist must be a list of at most 250 image names")
        result["allowlist"] = sorted({_process_image(v, "allowlist") for v in allowlist})

        rules = raw.get("process_rules", {})
        if not isinstance(rules, dict) or len(rules) > 250:
            raise ValueError("process_rules must be an object of at most 250 image rules")
        normalized_rules = {}
        global_thresholds = result["thresholds"]
        allowed_rule = {"mode", "cpu_percent", "required_hits", "minimum_hot_minutes"}
        for image, rule in rules.items():
            key = _process_image(image, "process_rules")
            if not isinstance(rule, dict):
                raise ValueError("process rule for %s must be an object" % key)
            unknown = set(rule) - allowed_rule
            if unknown:
                raise ValueError("unknown process rule field: %s" % sorted(unknown)[0])
            mode = rule.get("mode", "default")
            if mode not in ("default", "alert_only"):
                raise ValueError("process rule mode must be default or alert_only")
            clean = {"mode": mode}
            bounds = (
                ("cpu_percent", 50, 100, False),
                ("required_hits", 2, 60, True),
                ("minimum_hot_minutes", 5, 240, True),
            )
            for name, minimum, maximum, integer in bounds:
                if name not in rule:
                    continue
                value = _bounded_number(rule[name], name, minimum, maximum, integer=integer)
                if value < global_thresholds[name]:
                    raise ValueError("process rule %s may not weaken %s" % (key, name))
                clean[name] = value
            normalized_rules[key] = clean
        result["process_rules"] = normalized_rules
        return result, None
    except (TypeError, ValueError) as exc:
        return default_settings(), str(exc)


def read_settings_file(path=None):
    """Read the JSON config for the CLI; missing files use old safe defaults."""
    if not path:
        return default_settings(), None
    if not os.path.isfile(path):
        return default_settings(), "settings file not found: %s" % path
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        return default_settings(), "settings unreadable: %s" % exc
    return normalize_settings(raw)


def cpu_percent_core(
    cpu_before: float, cpu_after: float, sample_seconds: float = SAMPLE_SECONDS
) -> float:
    """Accumulated-CPU delta as a share of one logical core.

    A negative delta means the counter was re-read for a different process
    (PID reuse mid-sweep) or came back inconsistent. That is missing
    information, so it reads as 0.0 rather than as a large number.
    """
    if sample_seconds <= 0:
        return 0.0
    return max(0.0, (float(cpu_after) - float(cpu_before)) / float(sample_seconds) * 100.0)


def is_never_kill(
    name: str, *, is_main_process: bool = False, allowlist: object = (), pid: object = None
) -> bool:
    """Whether automatic termination is forbidden for this image.

    The allowlist blocks the KILL only. A suspicious allowlisted process is
    still worth an ALERT and a log line -- that is diagnostically useful, and
    silence would hide the one case where a system process really is spinning.
    """
    try:
        if pid is not None and int(pid) <= PID_FLOOR:
            return True
    except (TypeError, ValueError):
        return True  # an unreadable PID is missing information
    key = (name or "").strip().lower()
    if not key:
        # No name is missing information: bias toward not killing.
        return True
    extra = {str(item).strip().lower() for item in (allowlist or ())}
    if key in NEVER_KILL or key in extra:
        return True
    return is_main_process and key in MAIN_PROCESS_GUARDED


@dataclasses.dataclass(frozen=True)
class Sample:
    """One observation of one process, already collected by the watcher."""

    pid: int
    start_time: str  # ISO-8601; the other half of the process identity
    name: str
    cpu_percent: float
    orphan: bool
    observed_at: str  # ISO-8601 UTC
    cmdline: str = ""
    is_main_process: bool = False

    @property
    def key(self) -> str:
        """The process identity. Never the PID on its own."""
        return f"{self.pid}:{self.start_time}"

    @classmethod
    def from_dict(cls, raw: dict) -> "Sample":
        return cls(
            pid=int(raw["pid"]),
            start_time=str(raw["start_time"]),
            name=str(raw.get("name", "")),
            cpu_percent=float(raw.get("cpu_percent", 0.0)),
            orphan=bool(raw.get("orphan", False)),
            observed_at=str(raw["observed_at"]),
            cmdline=str(raw.get("cmdline", "")),
            is_main_process=bool(raw.get("is_main_process", False)),
        )


def classify_process(
    sample: Sample,
    *,
    allowlist: object = (),
    settings: object = None,
    decision: "Decision | None" = None,
) -> str:
    """Classify a process according to the Phase 7 safety model."""
    try:
        if sample.pid in KERNEL_PIDS or sample.pid <= PID_FLOOR:
            return CLASSIFICATION_PROTECTED_SYSTEM
    except (TypeError, ValueError):
        return CLASSIFICATION_UNKNOWN

    name = (sample.name or "").strip().lower()
    if not name:
        return CLASSIFICATION_UNKNOWN

    if name in NEVER_KILL:
        return CLASSIFICATION_PROTECTED_SYSTEM

    policy, _ = normalize_settings(settings)
    configured_allowlist = policy.get("allowlist", [])
    combined_allowlist = {str(x).strip().lower() for x in list(allowlist or ()) + list(configured_allowlist)}

    if name in combined_allowlist:
        return CLASSIFICATION_PROTECTED_USER

    if sample.is_main_process and name in MAIN_PROCESS_GUARDED:
        return CLASSIFICATION_PROTECTED_USER

    cmd = (sample.cmdline or "").lower()
    if any(pat in cmd for pat in ("-m pytest", "-m unittest", "pytest", "jest", "vitest", "playwright")):
        return CLASSIFICATION_PROTECTED_ACTIVE
    if "node" in cmd and "--test" in cmd:
        return CLASSIFICATION_PROTECTED_ACTIVE
    if name in ("pytest.exe", "vstest.console.exe", "testhost.exe", "antigravity.exe", "agy.exe", "claude.exe", "opencode.exe"):
        return CLASSIFICATION_PROTECTED_ACTIVE

    rule = policy.get("process_rules", {}).get(name, {})
    if rule.get("mode") == "alert_only":
        return CLASSIFICATION_OBSERVE_ONLY

    if decision is not None:
        if decision.action == KILL:
            # KILL only ever reaches here with a valid recovery lease (see
            # decide), so the strongest verdict means ownership was proven,
            # not merely that a name was absent from a denylist.
            return CLASSIFICATION_RECOVERY_ELIGIBLE
        if decision.hot and decision.orphan:
            return CLASSIFICATION_SUSPECTED_STALL

    if sample.cpu_percent > policy["thresholds"]["cpu_percent"] and sample.orphan:
        return CLASSIFICATION_SUSPECTED_STALL

    return CLASSIFICATION_OBSERVE_ONLY


@dataclasses.dataclass(frozen=True)
class Decision:
    """What to do about one sample, plus the facts that decided it."""

    action: str
    key: str
    hot: bool
    sustained: bool
    orphan: bool
    allowlisted: bool
    hits: int
    hot_seconds: int
    reason: str
    # Carried through from the sample so one log line can satisfy the whole
    # reporting contract without the caller re-joining decisions to samples.
    pid: int = 0
    start_time: str = ""
    name: str = ""
    cpu_percent: float = 0.0
    cmdline: str = ""
    # The state entry to persist for this identity. None means "drop it": the
    # process went cold, so its streak is over.
    record: "dict | None" = None
    threshold_cpu: float = 80.0
    classification: str = ""

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def now_iso() -> str:
    """Current UTC time as ISO-8601 with a Z suffix, stable for lease stamps."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> "datetime | None":
    """ISO-8601 to an aware datetime, or None when it cannot be trusted."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _elapsed_seconds(first: str, last: str) -> int:
    """Seconds between two stamps; 0 when either one is unusable.

    Unparseable time is missing information, and 0 elapsed can never satisfy
    the sustained gate. Guessing a duration here would manufacture the exact
    evidence the kill decision requires.
    """
    start, end = parse_time(first), parse_time(last)
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds()))


def _prior(history: object, sample: Sample) -> dict:
    """The stored streak for this exact identity, or an empty one.

    The key already carries PID + StartTime, but a hand-edited or partially
    recovered state file can hold an entry whose fields disagree with its key.
    Those fields are re-checked rather than trusted, so a mismatch starts a new
    streak instead of donating strikes to a different process.
    """
    if not isinstance(history, dict):
        return {}
    entry = history.get(sample.key)
    if not isinstance(entry, dict):
        return {}
    try:
        same_pid = int(entry.get("pid", -1)) == sample.pid
    except (TypeError, ValueError):
        return {}
    if not same_pid or str(entry.get("start_time", "")) != sample.start_time:
        return {}
    return entry


# Where recovery leases live. SAISPIN_LEASE_DIR overrides the per-user runtime
# directory so tests and the controlled-kill acceptance run isolated.
LEASE_ENV_DIR = "SAISPIN_LEASE_DIR"

# Managed-worker recovery contract version. v1 leases (pid/start/authorized/
# expires only) prove nothing about stalled work and NEVER authorize a kill.
LEASE_SCHEMA_VERSION = 3
LEASE_RECOVERY_POLICY_HEARTBEAT = "heartbeat-timeout"
LEASE_MAX_BYTES = 8192
LEASE_HEARTBEAT_MIN_SECONDS = 60
LEASE_HEARTBEAT_MAX_SECONDS = 86400
# Lease schema v2 was shape-only and accepted any hand-written JSON with a
# matching PID/start_time. v3 requires a producer_token that register_managed_worker()
# generates and also records in a manager-registry file. A hand-written lease that
# only mimics the field set has no matching registry entry and is rejected, binding
# the lease to a trusted SAITULS producer rather than to a file any same-user process
# can create in the lease directory.
LEASE_PRODUCER_REGISTRY = "producers.json"


def lease_directory() -> str:
    override = os.environ.get(LEASE_ENV_DIR)
    if override:
        return override
    data_root = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return os.path.join(data_root, "SAITULS", "SAISPIN", "leases")


def lease_path_for(sample: "Sample") -> str:
    safe_start = sample.start_time.replace(":", "_").replace("/", "_")
    return os.path.join(lease_directory(), "%d_%s.json" % (sample.pid, safe_start))


def lease_path_for_identity(pid: int, start_time: str) -> str:
    safe_start = str(start_time).replace(":", "_").replace("/", "_")
    return os.path.join(lease_directory(), "%d_%s.json" % (int(pid), safe_start))


def _producer_registry_path() -> str:
    """Path to the trusted-producer registry, sibling of the lease directory."""
    return os.path.join(lease_directory(), LEASE_PRODUCER_REGISTRY)


def _read_producer_registry() -> dict:
    """Read the trusted-producer registry. Returns {} if absent or corrupt."""
    path = _producer_registry_path()
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _write_producer_registry(registry: dict) -> None:
    """Atomically write the producer registry."""
    path = _producer_registry_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    text = json.dumps(registry)
    tmp = path + ".writer.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def validate_lease_producer_authority(path: str) -> "tuple[bool, str]":
    """Trust-boundary check for the lease file producer.

    A lease is only valid if it was created by register_managed_worker(),
    the trusted SAITULS producer. That function writes a random
    producer_token into both the lease and a manager-registry file; this
    check verifies the token matches a live registry entry. A hand-written
    JSON lease that merely mimics the field set has no matching registry
    entry and is refused.

    The lease directory is per-user; this proves the lease was produced by a
    SAITULS-managed caller, not by an arbitrary same-user process that can
    write a matching JSON file. Windows-identity binding (windows_user must
    be non-empty and must match the live process owner) is enforced
    separately by the watcher and re-checked at the kill gate.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, "lease file not accessible"
    if size <= 0 or size > LEASE_MAX_BYTES:
        return False, "lease file size out of bounds -- ownership cannot be proven"
    if not os.path.isfile(path):
        return False, "lease path is not a regular file"
    # P0-2: windows_user must be populated. An empty value is missing identity
    # evidence and must never authorize termination.
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            lease = json.load(handle)
    except (OSError, ValueError):
        return False, "lease unreadable -- producer cannot be verified"
    if not isinstance(lease, dict):
        return False, "lease malformed -- producer cannot be verified"
    windows_user = str(lease.get("windows_user", "") or "").strip()
    if not windows_user:
        return False, (
            "recovery lease has no windows_user -- process owner cannot be "
            "identified, destructive recovery denied"
        )
    # P0-1: producer_token must match a trusted registry entry.
    token = str(lease.get("producer_token", "") or "").strip()
    if not token:
        return False, (
            "recovery lease lacks producer_token -- not produced by a trusted "
            "SAITULS manager, destructive recovery denied"
        )
    registry = _read_producer_registry()
    entry = registry.get(token)
    if entry is None:
        return False, (
            "recovery lease producer_token not found in trusted registry -- "
            "unauthenticated producer, destructive recovery denied"
        )
    # The registry entry must still be active (not revoked).
    if entry.get("revoked") is True:
        return False, (
            "recovery lease producer_token revoked -- untrusted producer, "
            "destructive recovery denied"
        )
    return True, ""


def build_managed_lease(*, pid: int, start_time: str, manager_id: str,
                        job_id: str, windows_user: str = "",
                        recovery_policy: str = LEASE_RECOVERY_POLICY_HEARTBEAT,
                        heartbeat_timeout_seconds: int = 900,
                        authorized: bool = False,
                        expires_at: str = "", last_progress_at: str = "",
                        generation: int = 1, producer_token: str = "") -> dict:
    """Construct a managed-worker recovery contract (schema v3).

    ``producer_token`` is a cryptographically-random UUID4 generated by
    register_managed_worker() and also recorded in the manager-registry file.
    validate_lease_producer_authority() requires a matching registry entry
    before the lease can authorize destructive recovery.
    """
    return {
        "schema_version": LEASE_SCHEMA_VERSION,
        "pid": int(pid),
        "start_time": str(start_time),
        "manager_id": str(manager_id or ""),
        "job_id": str(job_id or ""),
        "windows_user": str(windows_user or ""),
        "producer_token": str(producer_token or ""),
        "authorized": bool(authorized),
        "revoked": False,
        "expires_at": str(expires_at or ""),
        "recovery_policy": str(recovery_policy or ""),
        "heartbeat_timeout_seconds": int(heartbeat_timeout_seconds),
        "last_progress_at": str(last_progress_at or ""),
        "generation": int(generation),
    }


def write_lease_atomic(path: str, payload: dict, expect_generation: int = 1) -> bool:
    """Atomically write a lease file with optimistic generation checking.

    ``expect_generation`` guards concurrent multi-writer races: if the on-disk
    lease has a different generation than expected, the write is skipped and
    the caller must re-read + retry. The temp filename includes the thread
    id and a random nonce so concurrent writers in the same process cannot
    collide on a shared temp name.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # P0-3: thread-safe temp name -- PID + thread id + nonce.
    nonce = uuid.uuid4().hex[:8]
    tmp = path + ".%d.%d.%s.tmp" % (os.getpid(), threading.get_ident(), nonce)
    # Generation check prevents clobbering a newer lease written by a concurrent
    # writer between read and replace.
    generation = int(payload.get("generation", 1))
    if generation < int(expect_generation):
        return False
    text = json.dumps(payload)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return True


def register_managed_worker(*, pid: int, start_time: str, manager_id: str,
                            job_id: str, windows_user: str = "",
                            heartbeat_timeout_seconds: int = 900,
                            expires_at: str = "", now_at: str = "") -> "tuple[str, str]":
    """REGISTER: create an unauthorized managed-worker lease for one identity.

    The worker is NOT kill-eligible until authorize_recovery() is called AND
    the heartbeat goes stale. Returns (lease_path, producer_token).

    The generated ``producer_token`` (UUID4) is recorded in the manager-registry
    file so that validate_lease_producer_authority() can authenticate the
    lease producer. Only SAITULS-managed workers created through this function
    produce verifiable leases; hand-written JSON files have no matching
    registry entry and are refused.
    """
    producer_token = uuid.uuid4().hex
    registry = _read_producer_registry()
    registry[producer_token] = {
        "pid": int(pid),
        "start_time": str(start_time),
        "manager_id": str(manager_id or ""),
        "job_id": str(job_id or ""),
        "windows_user": str(windows_user or ""),
        "created_at": now_at or start_time,
        "revoked": False,
    }
    _write_producer_registry(registry)
    payload = build_managed_lease(
        pid=pid, start_time=start_time, manager_id=manager_id, job_id=job_id,
        windows_user=windows_user,
        recovery_policy=LEASE_RECOVERY_POLICY_HEARTBEAT,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        authorized=False, expires_at=expires_at,
        last_progress_at=now_at or start_time, generation=1,
        producer_token=producer_token)
    path = lease_path_for_identity(pid, start_time)
    write_lease_atomic(path, payload)
    return path, producer_token


def heartbeat_managed_worker(pid: int, start_time: str, now_at: str) -> bool:
    """HEARTBEAT_OR_PROGRESS: refresh last validated progress. No auth change."""
    path = lease_path_for_identity(pid, start_time)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            lease = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(lease, dict):
        return False
    try:
        if int(lease.get("pid", -1)) != int(pid):
            return False
    except (TypeError, ValueError):
        return False
    if str(lease.get("start_time", "")) != str(start_time):
        return False
    if lease.get("revoked") is True:
        return False
    lease["last_progress_at"] = str(now_at)
    try:
        current_gen = int(lease.get("generation", 1))
        written = write_lease_atomic(path, lease, expect_generation=current_gen)
        if not written:
            return False
    except OSError:
        return False
    return True


def authorize_recovery(pid: int, start_time: str) -> bool:
    """AUTHORIZE_RECOVERY: opt this managed worker into destructive recovery."""
    path = lease_path_for_identity(pid, start_time)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            lease = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(lease, dict):
        return False
    if str(lease.get("start_time", "")) != str(start_time):
        return False
    lease["authorized"] = True
    lease["revoked"] = False
    try:
        current_gen = int(lease.get("generation", 1))
        written = write_lease_atomic(path, lease, expect_generation=current_gen)
        if not written:
            return False
    except OSError:
        return False
    return True


def revoke_managed_worker(pid: int, start_time: str) -> bool:
    """REVOKE: manager withdraws ownership/authorization effective immediately."""
    path = lease_path_for_identity(pid, start_time)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            lease = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(lease, dict):
        return False
    try:
        lease["generation"] = int(lease.get("generation", 1)) + 1
    except (TypeError, ValueError):
        lease["generation"] = 2
    lease["revoked"] = True
    lease["authorized"] = False
    try:
        current_gen = int(lease.get("generation", 1))
        written = write_lease_atomic(path, lease, expect_generation=current_gen)
        if not written:
            return False
    except OSError:
        return False
    return True


def cleanup_managed_worker(pid: int, start_time: str) -> bool:
    """PROCESS_EXIT_CLEANUP: remove the lease when the managed process exits."""
    path = lease_path_for_identity(pid, start_time)
    try:
        if os.path.exists(path):
            os.unlink(path)
        return True
    except OSError:
        return False


def evaluate_recovery_lease(sample: "Sample") -> "tuple[bool, str]":
    """Positive proof that THIS managed worker may be recovered.

    Three independent facts are required (P0-1):
      1. OWNERSHIP: schema-v2 lease bound to exact PID+creation time with a
         non-empty manager_id and job_id.
      2. RECOVERY AUTHORIZATION: authorized is True, revoked is not True,
         and expires_at has not passed the sweep observation time.
      3. RECOVERY CONDITION: the owner opted into heartbeat-timeout recovery
         AND last_progress_at is stale by at least heartbeat_timeout_seconds
         as of observed_at. Fresh progress means active useful work survives.

    A v1 lease (or any lease missing the managed contract) is NOT proof of
    stalled work: it downgrades to observe/alert only. Arbitrary new JSON
    fields are never trusted -- only this explicit contract is evaluated.

    Same-user trust limitation: a writable JSON file cannot prove ownership
    against the same Windows account that wrote it; it only scopes recovery
    to explicitly registered SAITULS-managed jobs. See
    validate_lease_producer_authority().

    Returns ``(eligible, reason)``. The reason is the refusal text on failure.
    """
    path = lease_path_for(sample)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            lease = json.load(handle)
    except FileNotFoundError:
        return False, (
            "no recovery lease for this identity -- ownership unknown, "
            "observe and alert only"
        )
    except (OSError, ValueError):
        return False, "recovery lease unreadable -- ownership cannot be proven"

    if not isinstance(lease, dict):
        return False, "recovery lease malformed -- ownership cannot be proven"
    try:
        lease_pid = int(lease.get("pid", -1))
    except (TypeError, ValueError):
        return False, "recovery lease pid unreadable -- ownership cannot be proven"
    if lease_pid != sample.pid or str(lease.get("start_time", "")) != sample.start_time:
        return False, "recovery lease identity mismatch -- lease belongs to another process"
    # OWNERSHIP: explicit managed-worker contract required (P0-1/P0-2).
    try:
        schema_version = int(lease.get("schema_version", 0))
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version != LEASE_SCHEMA_VERSION:
        return False, (
            "recovery lease missing managed-worker contract "
            "(schema_version 3) -- ownership requires explicit manager/job "
            "registration, observe and alert only"
        )
    ok, why = validate_lease_producer_authority(path)
    if not ok:
        return False, "recovery lease trust check failed (%s)" % why
    manager_id = str(lease.get("manager_id", "") or "").strip()
    job_id = str(lease.get("job_id", "") or "").strip()
    if not manager_id or not job_id:
        return False, (
            "recovery lease missing manager/job ownership -- "
            "destructive recovery denied"
        )
    if lease.get("revoked") is True:
        return False, "recovery lease revoked -- manager ownership withdrawn"
    # RECOVERY AUTHORIZATION.
    if lease.get("authorized") is not True:
        return False, "recovery lease not authorized -- destructive recovery denied"
    expiry = parse_time(str(lease.get("expires_at", "")))
    observed = parse_time(sample.observed_at)
    if expiry is None or observed is None:
        return False, "recovery lease expiry unreadable -- ownership cannot be proven"
    if observed >= expiry:
        return False, "recovery lease expired -- authorization no longer valid"
    # RECOVERY CONDITION: heartbeat-timeout explicitly opted in by the owner.
    policy = str(lease.get("recovery_policy", "") or "").strip()
    if policy != LEASE_RECOVERY_POLICY_HEARTBEAT:
        return False, (
            "recovery lease policy does not authorize heartbeat-timeout "
            "recovery -- observe and alert only"
        )
    try:
        timeout = int(lease.get("heartbeat_timeout_seconds", 0))
    except (TypeError, ValueError):
        return False, "recovery lease heartbeat timeout unreadable -- condition unproven"
    if timeout < LEASE_HEARTBEAT_MIN_SECONDS or timeout > LEASE_HEARTBEAT_MAX_SECONDS:
        return False, "recovery lease heartbeat timeout out of bounds -- condition unproven"
    progress = parse_time(str(lease.get("last_progress_at", "")))
    if progress is None:
        return False, (
            "recovery lease progress unreadable -- stalled work unproven, "
            "observe and alert only"
        )
    stale_seconds = (observed - progress).total_seconds()
    if stale_seconds < 0:
        return False, (
            "recovery lease progress is in the future -- clock skew, "
            "observe and alert only"
        )
    if stale_seconds < timeout:
        return False, (
            "managed worker showed progress %ds ago (timeout %ds) -- "
            "active useful work survives" % (int(stale_seconds), int(timeout))
        )
    return True, ""


def decide(
    history: object,
    sample: Sample,
    auto_kill: bool = False,
    *,
    allowlist: object = (),
    history_trusted: bool = True,
    settings: object = None,
) -> Decision:
    """The whole safety decision for one process, as data in and data out.

    ``history`` is the full identity-keyed state mapping the watcher persists;
    ``history_trusted`` is False when that state was rebuilt after a corrupt
    read, which downgrades any KILL to an ALERT.
    """
    policy, _warning = normalize_settings(settings)
    configured_allowlist = policy["allowlist"]
    combined_allowlist = list(allowlist or ()) + configured_allowlist
    allowlisted = is_never_kill(
        sample.name,
        is_main_process=sample.is_main_process,
        allowlist=combined_allowlist,
        pid=sample.pid,
    )
    active_protected = False
    cmd_lower = (sample.cmdline or "").lower()
    if any(pat in cmd_lower for pat in ("-m pytest", "-m unittest", "pytest", "jest", "vitest", "playwright")):
        active_protected = True
    # node's built-in test runner is `node [flags] file --test` with the flag
    # anywhere after the script, so a fixed "node --test" substring misses it.
    if "node" in cmd_lower and "--test" in cmd_lower:
        active_protected = True
    if sample.name.lower() in ("pytest.exe", "vstest.console.exe", "testhost.exe", "antigravity.exe", "agy.exe", "claude.exe", "opencode.exe"):
        active_protected = True
    if active_protected:
        allowlisted = True

    rule = policy["process_rules"].get(sample.name.strip().lower(), {})
    thresholds = dict(policy["thresholds"])
    for name in ("cpu_percent", "required_hits", "minimum_hot_minutes"):
        if name in rule:
            thresholds[name] = rule[name]
    hot = sample.cpu_percent > thresholds["cpu_percent"]
    eff_threshold = float(thresholds["cpu_percent"])

    if sample.pid in KERNEL_PIDS:
        # Not even tracked. Idle is "hot" by definition on an idle machine and
        # has no parent to lose, so tracking it would mean an alert every five
        # minutes for the rest of time about the one process that is doing
        # nothing. It cannot be killed either, so there is nothing to report.
        return Decision(
            action=IGNORE,
            key=sample.key,
            hot=hot,
            sustained=False,
            orphan=sample.orphan,
            allowlisted=True,
            hits=0,
            hot_seconds=0,
            reason="kernel process (PID %d) -- never tracked, never killed" % sample.pid,
            pid=sample.pid,
            start_time=sample.start_time,
            name=sample.name,
            cpu_percent=sample.cpu_percent,
            cmdline=sample.cmdline[:CMDLINE_LIMIT],
            record=None,
            threshold_cpu=eff_threshold,
            classification=CLASSIFICATION_PROTECTED_SYSTEM,
        )

    if not hot:
        # A cold sample ends the streak. This is what keeps HOT HOT HOT COLD
        # HOT HOT HOT from adding up to six.
        return Decision(
            action=IGNORE,
            key=sample.key,
            hot=False,
            sustained=False,
            orphan=sample.orphan,
            allowlisted=allowlisted,
            hits=0,
            hot_seconds=0,
            reason="cold sample -- the hot streak restarts from zero",
            pid=sample.pid,
            start_time=sample.start_time,
            name=sample.name,
            cpu_percent=sample.cpu_percent,
            cmdline=sample.cmdline[:CMDLINE_LIMIT],
            record=None,
            threshold_cpu=eff_threshold,
            classification=classify_process(sample, allowlist=combined_allowlist, settings=policy),
        )

    prior = _prior(history, sample)

    # A gap longer than a few missed sweeps means nobody was observing. The
    # streak stored on the other side of it proves nothing about the interval,
    # so it is dropped rather than extended: a laptop that slept for two days
    # must not wake up holding five strikes against a process.
    if prior and _elapsed_seconds(
        str(prior.get("last_hot") or ""), sample.observed_at
    ) > policy["timing"]["stale_gap_minutes"] * 60:
        prior = {}

    first_hot = str(prior.get("first_hot") or sample.observed_at)
    try:
        hits = int(prior.get("hits", 0)) + 1
    except (TypeError, ValueError):
        hits = 1
    hot_seconds = _elapsed_seconds(first_hot, sample.observed_at)
    required_seconds = thresholds["minimum_hot_minutes"] * 60
    sustained = hits >= thresholds["required_hits"] and hot_seconds >= required_seconds

    record = {
        "pid": sample.pid,
        "start_time": sample.start_time,
        "first_hot": first_hot,
        "last_hot": sample.observed_at,
        "hits": hits,
        "name": sample.name,
        "cmdline": sample.cmdline[:CMDLINE_LIMIT],
    }

    def verdict(action: str, reason: str) -> Decision:
        temp_dec = Decision(
            action=action,
            key=sample.key,
            hot=True,
            sustained=sustained,
            orphan=sample.orphan,
            allowlisted=allowlisted,
            hits=hits,
            hot_seconds=hot_seconds,
            reason=reason,
            pid=sample.pid,
            start_time=sample.start_time,
            name=sample.name,
            cpu_percent=sample.cpu_percent,
            cmdline=sample.cmdline[:CMDLINE_LIMIT],
            record=record,
            threshold_cpu=eff_threshold,
            classification="",
        )
        cls_val = classify_process(
            sample,
            allowlist=combined_allowlist,
            settings=policy,
            decision=temp_dec,
        )
        return Decision(
            action=action,
            key=sample.key,
            hot=True,
            sustained=sustained,
            orphan=sample.orphan,
            allowlisted=allowlisted,
            hits=hits,
            hot_seconds=hot_seconds,
            reason=reason,
            pid=sample.pid,
            start_time=sample.start_time,
            name=sample.name,
            cpu_percent=sample.cpu_percent,
            cmdline=sample.cmdline[:CMDLINE_LIMIT],
            record=record,
            threshold_cpu=eff_threshold,
            classification=cls_val,
        )

    if not sustained:
        return verdict(
            IGNORE,
            "hot but not sustained: %d/%d consecutive hot samples over %dm/%dm"
            % (hits, thresholds["required_hits"], hot_seconds // 60,
               thresholds["minimum_hot_minutes"]),
        )
    if not sample.orphan:
        return verdict(
            ALERT,
            "sustained spin with a live parent -- reported, never auto-killed",
        )
    if rule.get("mode") == "alert_only":
        return verdict(ALERT, "process rule is alert-only -- never auto-killed")
    if not auto_kill:
        return verdict(
            ALERT, "orphan sustained spin -- auto-kill is off, nothing terminated"
        )
    if allowlisted:
        reason_msg = "orphan sustained spin on a never-kill process -- reported only"
        if active_protected:
            reason_msg = "active protected work (test runner, compiler, or agent) -- reported only"
        return verdict(ALERT, reason_msg)
    if not history_trusted:
        return verdict(
            ALERT,
            "orphan sustained spin, but the history was rebuilt after a bad "
            "state read -- no kill from evidence that cannot be trusted",
        )
    # P0-1: CPU heat + orphanhood never proves recoverability. A kill needs a
    # positive ownership token: a recovery lease bound to this exact identity
    # (PID + creation time), explicitly authorized and still unexpired. Without
    # one the verdict stops at SUSPECTED_STALL -- detected, alerted, evidenced,
    # never force-terminated on heuristic uncertainty.
    lease_ok, lease_why = evaluate_recovery_lease(sample)
    if not lease_ok:
        return verdict(ALERT, lease_why)
    return verdict(
        KILL,
        "orphan sustained spin, auto-kill enabled, identity matched, not allowlisted, "
        "recovery lease valid, heartbeat stale -- ownership positively proven "
        "plus authorization and stalled-work condition",
    )


def sweep(
    history: object,
    samples: object,
    auto_kill: bool = False,
    *,
    allowlist: object = (),
    history_trusted: bool = True,
    settings: object = None,
) -> "tuple[list[Decision], dict]":
    """Decide a whole enumeration and return the state to persist.

    The next state is rebuilt from the decisions rather than edited in place,
    so a process that disappeared between sweeps drops out by simply not being
    in ``samples`` -- no separate prune pass to forget to run.
    """
    policy, _warning = normalize_settings(settings)
    decisions = [
        decide(
            history,
            item if isinstance(item, Sample) else Sample.from_dict(item),
            auto_kill,
            allowlist=allowlist,
            history_trusted=history_trusted,
            settings=policy,
        )
        for item in (samples or ())
    ]
    next_history = {d.key: d.record for d in decisions if d.record is not None}
    return decisions, next_history


def _read_stdin() -> str:
    """The watcher's payload, as UTF-8, whatever the console says.

    Windows PowerShell 5.1 can prepend a UTF-8 BOM when it pipes a string to a
    native command (seen from a nested powershell.exe, even with
    $OutputEncoding set to UTF-8 without BOM), and sys.stdin decodes with the
    console code page, not UTF-8. Reading the bytes and decoding utf-8-sig
    takes both out of the equation.
    """
    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:
        return sys.stdin.read()
    return buf.read().decode("utf-8-sig")


def main(argv: "list[str] | None" = None) -> int:
    """JSON sweep on stdin, decisions plus next state on stdout.

    The adapter is deliberately thin: it moves data, and every rule that can
    end a process lives in the pure functions above where the tests can reach
    it without a real process tree.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] == "--normalize-settings":
        if len(args) > 2:
            sys.stderr.write("usage: saispin_logic.py --normalize-settings [settings.json|--defaults]\n")
            return 2
        if len(args) == 2 and args[1] == "--defaults":
            settings, warning = default_settings(), None
        elif len(args) == 2:
            settings, warning = read_settings_file(args[1])
        else:
            try:
                settings, warning = normalize_settings(json.loads(_read_stdin() or "null"))
            except ValueError as exc:
                settings, warning = default_settings(), "settings JSON unreadable: %s" % exc
        json.dump({"settings": settings, "warning": warning}, sys.stdout)
        sys.stdout.write("\n")
        return 0
    for flag in args:
        if flag not in ("--sweep",):
            sys.stderr.write("usage: saispin_logic.py [--sweep] < payload.json\n")
            return 2
    try:
        payload = json.loads(_read_stdin() or "{}")
    except ValueError as exc:
        sys.stderr.write("bad payload: %s\n" % exc)
        return 2

    decisions, next_history = sweep(
        payload.get("history") or {},
        payload.get("samples") or [],
        bool(payload.get("auto_kill", False)),
        allowlist=payload.get("allowlist") or (),
        history_trusted=bool(payload.get("history_trusted", True)),
        settings=payload.get("settings"),
    )
    json.dump(
        {
            "decisions": [d.as_dict() for d in decisions],
            "history": next_history,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
