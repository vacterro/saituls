"""Structured delete failure taxonomy for DEL JUNK.

Production safety decisions classify failures by exception type and structured
properties (errno, winerror), never by exception message text.

Classifications:

    DELETE_RECOVERABLE_BUSY   ordinary Windows contention: locked, in-use,
                              access denied, sharing violation.  The candidate
                              is not evidence that the whole machine must stop.

    DELETE_CHANGED            the candidate vanished or changed identity between
                              INTENT and disposition.

    DELETE_PARTIAL_BUSY       a tree delete partially mutated before a
                              recoverable child failure.

    DELETE_FATAL              unknown, device-level, or filesystem integrity
                              failure.  The run must stop globally.
"""

import errno as _errno

# ── Classification constants ────────────────────────────────────────────────

DELETE_RECOVERABLE_BUSY = "DELETE_RECOVERABLE_BUSY"
DELETE_CHANGED = "DELETE_CHANGED"
DELETE_PARTIAL_BUSY = "DELETE_PARTIAL_BUSY"
DELETE_FATAL = "DELETE_FATAL"

# ── Windows error codes that are ordinary contention / race ─────────────────
#
# Conservative: only codes that are well-understood benign contention.
# Unknown codes fall through to DELETE_FATAL (fail closed).

_RECOVERABLE_WINERRORS = frozenset({
    2,    # ERROR_FILE_NOT_FOUND
    3,    # ERROR_PATH_NOT_FOUND
    5,    # ERROR_ACCESS_DENIED
    32,   # ERROR_SHARING_VIOLATION
    33,   # ERROR_LOCK_VIOLATION
    145,  # ERROR_DIR_NOT_EMPTY
})

# POSIX errno values that map to recoverable contention.
_RECOVERABLE_ERRNOS = frozenset({
    _errno.ENOENT,
    _errno.EACCES,
    getattr(_errno, "EPERM", 1),
})


def classify_delete_error(exc):
    """Classify an OSError by its structured properties.

    Returns one of DELETE_RECOVERABLE_BUSY, DELETE_CHANGED, DELETE_FATAL.
    Never inspects ``str(exc)`` or ``exc.args`` text.
    """
    if isinstance(exc, FileNotFoundError):
        return DELETE_CHANGED

    if isinstance(exc, PermissionError):
        # PermissionError is errno EACCES or EPERM on Windows; the winerror
        # is typically ERROR_ACCESS_DENIED (5).  Always recoverable.
        return DELETE_RECOVERABLE_BUSY

    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        if winerror is not None:
            if winerror in _RECOVERABLE_WINERRORS:
                return DELETE_RECOVERABLE_BUSY
            # A winerror we don't recognise: fail closed.
            return DELETE_FATAL
        # Non-Windows: fall back to errno.
        errno_val = getattr(exc, "errno", None)
        if errno_val is not None and errno_val in _RECOVERABLE_ERRNOS:
            return DELETE_RECOVERABLE_BUSY

    return DELETE_FATAL


class RecoverableTreeDeleteError(OSError):
    """A tree deletion encountered a recoverable child failure.

    ``mutated`` is True when at least one child file or directory was
    successfully removed before the failure.  ``failing_path`` is the
    path of the child that could not be removed.
    """

    def __init__(self, original_error, *, mutated, failing_path):
        super().__init__(str(original_error))
        self.original_error = original_error
        self.mutated = mutated
        self.failing_path = failing_path
