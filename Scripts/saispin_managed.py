#!/usr/bin/env python3
"""SAITULS-managed-worker registration for SAISPIN.

SAITULS tools that run as long-lived disposable workers call
``register_self()`` at startup so the SAISPIN watchdog can recognize them
as trusted managed workers with a recovery lease. The lease is unauthorized
by default: the worker must explicitly call ``authorize_self()`` before the
watchdog can terminate it for a sustained spin after parent loss.

The producer_token generated here is also recorded in the manager-registry
file (``producers.json``) inside the lease directory, so only SAITULS-produced
leases pass ``validate_lease_producer_authority()``. A hand-written JSON lease
that merely mimics the field set has no matching registry entry and is refused.

Typical usage inside a SAITULS worker script:

    from Scripts.saispin_managed import register_self, authorize_self, heartbeat

    pid, start = register_self(__file__)
    # ... do work ...
    authorize_self(pid, start)
    try:
        for item in workload:
            # ... process item ...
            heartbeat(pid, start)
    finally:
        unregister_self(pid, start)
"""
import os
import sys
import uuid

# Import the decision engine's lease management surface.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
import saispin_logic as logic


def register_self(job_name: str = "") -> "tuple[int, str, str]":
    """Register the CURRENT process with SAISPIN.

    Returns (pid, start_time, producer_token). The caller should record and
    pass pid/start_time back to the other helpers. The lease is created with
    ``authorized=False`` and ``manager_id`` set to ``saituls-managed``.
    """
    pid = os.getpid()
    start_time = logic.now_iso()
    producer_token = uuid.uuid4().hex
    manager_id = "saituls-managed"
    job_id = job_name or "sa-ituls-%d" % pid
    windows_user = "%s\\%s" % (
        os.environ.get("USERDOMAIN", ""),
        os.environ.get("USERNAME", ""),
    )
    path, _token = logic.register_managed_worker(
        pid=pid,
        start_time=start_time,
        manager_id=manager_id,
        job_id=job_id,
        windows_user=windows_user,
        expires_at="",
        now_at=start_time,
    )
    # register_managed_worker already wrote the producer_token into both the
    # lease and the registry. Return it for the caller's bookkeeping.
    return pid, start_time, _token


def authorize_self(pid: int, start_time: str) -> bool:
    """Opt self into destructive recovery. Call when work begins."""
    return logic.authorize_recovery(pid, start_time)


def heartbeat(pid: int, start_time: str) -> bool:
    """Refresh last-progress timestamp. Call periodically during work."""
    return logic.heartbeat_managed_worker(pid, start_time, logic.now_iso())


def revoke_self(pid: int, start_time: str) -> bool:
    """Withdraw recovery authorization immediately."""
    return logic.revoke_managed_worker(pid, start_time)


def unregister_self(pid: int, start_time: str) -> bool:
    """Remove the lease on clean exit."""
    return logic.cleanup_managed_worker(pid, start_time)


if __name__ == "__main__":
    # Smoke test: register, authorize, revoke, cleanup.
    pid, start, token = register_self("saispin_managed-smoke")
    print("registered pid=%d start=%s token=%s" % (pid, start, token[:8] + "..."))
    assert logic.authorize_recovery(pid, start), "authorize failed"
    print("authorized")
    assert logic.heartbeat_managed_worker(pid, start, logic.now_iso()), "heartbeat failed"
    print("heartbeat ok")
    assert logic.revoke_managed_worker(pid, start), "revoke failed"
    assert logic.cleanup_managed_worker(pid, start), "cleanup failed"
    print("revoked + cleaned up")
    print("SMOKE OK")
