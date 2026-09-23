"""Lifecycle audit log for SAITULS Secure Apps -- allowlist, not denylist.

A redaction filter that searches for known secrets is the wrong shape: it can
only remove what it has been told about, and a secret that reaches the logger
through an exception message it never saw is logged in full. This logger works
the other way round. A record is built from a fixed set of permitted fields,
every value is coerced to a short scalar, and anything else is dropped.

Permitted:   profile id, transition, timestamp, process id, mount target,
             auth provider name, credential identifier hash, result,
             error category.

Forbidden:   FIDO2 hmac-secret output, BitLocker password / recovery secret,
             PIN, raw credential private material, plaintext vault content,
             Claude credentials, authentication tokens.

Exception objects never reach the log. Callers pass an ``error_category``
from :data:`ERROR_CATEGORIES`; the free text of an exception is exactly the
place a secret leaks from, so it is not written at all.
"""
import json
import ntpath
import os
import threading
import time

ALLOWED_FIELDS = (
    "ts",
    "profile_id",
    "transition",
    "state",
    "pid",
    "mount_path",
    "auth_provider",
    "credential_id_hash",
    "result",
    "error_category",
    "detail_code",
    "container_id",
    "backend",
    "policy_mode",
    "event",
    "seq",
)

RESULTS = ("ok", "failed", "refused", "cancelled", "timeout", "noop")

ERROR_CATEGORIES = (
    "none",
    "config_invalid",
    "path_policy",
    "auth_capability",
    "auth_cancelled",
    "auth_wrong_credential",
    "auth_unavailable",
    "auth_failed",
    "unwrap_failed",
    "storage_create_failed",
    "storage_unlock_failed",
    "storage_mount_failed",
    "storage_unmount_failed",
    "storage_busy",
    "app_launch_failed",
    "app_exit_timeout",
    "recovery_required",
    "session_expired",
    "already_running",
    "concurrent_request",
    "not_enrolled",
    "hardware_acceptance_required",
    # The pre-authorized elevation boundary (sa_privtask.py). Distinct from
    # the storage categories on purpose: "the volume would not unlock" and
    # "the elevated helper could not be started at all" need different
    # answers from the person reading the log.
    "privileged_helper_unavailable",
    "privileged_task_missing",
    "privileged_task_tampered",
    "internal",
)

MAX_SCALAR_CHARS = 260


def _scalar(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    text = str(value)
    if len(text) > MAX_SCALAR_CHARS:
        text = text[:MAX_SCALAR_CHARS - 3] + "..."
    return text


class AuditLog(object):
    """Append-only JSONL audit sink. Thread-safe; never raises to the caller."""

    def __init__(self, path, echo=None, max_bytes=2 * 1024 * 1024):
        self.path = path
        self.echo = echo
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._seq = 0
        self.records = []          # in-memory tail, used by the GUI and tests
        self.max_records = 500

    def _rotate_if_needed(self):
        try:
            if self.max_bytes and os.path.getsize(self.path) > self.max_bytes:
                backup = self.path + ".1"
                try:
                    if os.path.exists(backup):
                        os.remove(backup)
                except OSError:
                    pass
                os.replace(self.path, backup)
        except OSError:
            pass

    def write(self, transition, profile_id=None, result="ok", **fields):
        """Record one lifecycle transition. Unknown fields are dropped silently.

        Silently, because a caller that accidentally passes ``secret=...``
        must not turn the mistake into an exception carrying the value.
        """
        if result not in RESULTS:
            result = "failed"
        category = fields.get("error_category") or "none"
        if category not in ERROR_CATEGORIES:
            category = "internal"
        with self._lock:
            self._seq += 1
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "seq": self._seq,
                "transition": _scalar(transition),
                "result": result,
                "error_category": category,
            }
            if profile_id is not None:
                record["profile_id"] = _scalar(profile_id)
            for key, value in fields.items():
                if key in ("error_category",):
                    continue
                if key not in ALLOWED_FIELDS:
                    continue
                record[key] = _scalar(value)
            ordered = {k: record[k] for k in ALLOWED_FIELDS if k in record}
            self.records.append(ordered)
            if len(self.records) > self.max_records:
                del self.records[:len(self.records) - self.max_records]
            line = json.dumps(ordered, sort_keys=False, separators=(",", ":"))
            try:
                directory = ntpath.dirname(self.path)
                if directory and not os.path.isdir(directory):
                    os.makedirs(directory, exist_ok=True)
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            except OSError:
                # An unwritable audit file must not take the broker down; the
                # in-memory tail still feeds the window.
                pass
            if self.echo is not None:
                try:
                    self.echo(line)
                except Exception:
                    pass
            return ordered

    def tail(self, count=50):
        with self._lock:
            return list(self.records[-count:])


class NullAuditLog(AuditLog):
    """Memory-only sink for tests and for a dry-run CLI."""

    def __init__(self):
        AuditLog.__init__(self, path=os.devnull, echo=None, max_bytes=0)

    def write(self, transition, profile_id=None, result="ok", **fields):
        record = AuditLog.write(self, transition, profile_id=profile_id,
                                result=result, **fields)
        return record
