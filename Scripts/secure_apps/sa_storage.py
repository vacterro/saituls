"""Storage backends for SAITULS Secure Apps.

A backend answers: *where does the protected data live, and is it exposed
right now?* It knows nothing about FIDO2, sessions or application lifetime.
Adding a second backend (a different container format, a network volume, a
different encryption product) means implementing this interface and adding
its name to :data:`sa_config.PRODUCTION_BACKENDS` -- no broker change.

The three states a container can be in are deliberately distinct:

``detached``
    The container file exists but no volume is attached. This is LOCKED.

``attached_locked``
    The VHDX is attached but BitLocker has not released the volume. Data is
    unreadable, but a previous detach did not complete -- a recovery
    condition, not a resting state.

``mounted``
    The plaintext filesystem is reachable at the mount path. Every second in
    this state is a second the vault is readable, which is why the default
    policy unmounts the moment the protected application exits, even though
    the authentication session stays valid for longer.

``missing`` is reported when no container exists yet, so enrollment can tell
"never created" apart from "locked".
"""
import ntpath
import os
import threading

import sa_paths
import sa_privhelper

DETACHED = "detached"
ATTACHED_LOCKED = "attached_locked"
MOUNTED = "mounted"
MISSING = "missing"


class StorageError(Exception):
    category = "storage_mount_failed"

    def __init__(self, message, category=None):
        Exception.__init__(self, message)
        if category:
            self.category = category


class StorageBusyError(StorageError):
    category = "storage_busy"


class SecureStorageBackend(object):
    """Interface every storage backend implements."""

    name = "abstract"
    requires_elevation = False

    @staticmethod
    def assert_mount_target(profile, what="mount target"):
        """Refuse a mount path that is not a provably empty directory.

        Every backend that attaches a volume to a directory shares this
        precondition, and every one of them must check it before it starts
        mutating, not after. See :func:`sa_paths.assert_directory_empty`.
        """
        try:
            return sa_paths.assert_directory_empty(profile.mount_path, what)
        except sa_paths.PathPolicyError as exc:
            raise StorageError(str(exc), "storage_create_failed")

    def start(self):
        """Acquire whatever privileged channel this backend needs."""

    def stop(self):
        """Release it. Must be safe to call twice."""

    def state(self, profile):
        raise NotImplementedError

    def create(self, profile, volume_secret):
        """Create the container. Returns a dict with ``recovery_password``.

        The recovery material is returned exactly once, to the caller, for a
        one-time display. It is never written next to the container.
        """
        raise NotImplementedError

    def unlock_and_mount(self, profile, volume_secret):
        raise NotImplementedError

    def unmount(self, profile):
        raise NotImplementedError

    def mounted_at(self, profile):
        """Where the container's volume is mounted right now, or None.

        Physical truth for recovery decisions: a journal saying "mounted"
        once is history, this is the present. A backend that cannot tell
        returns None, which callers must treat as "not proven".
        """
        return None

    def destroy(self, profile):
        """Remove a container that must not exist any more. Rollback only.

        The backend owns its container, so unwinding a half-finished
        enrollment is its job, not the caller guessing at a file path. Never
        used on an enrolled vault: the only caller is the rollback that
        follows a refused acknowledgement.
        """
        raise NotImplementedError

    def mount_is_live(self, profile):
        """Cheap filesystem-level confirmation that the mount really carries data."""
        try:
            return os.path.isdir(profile.mount_path) and not sa_paths.directory_is_empty(
                profile.mount_path)
        except OSError:
            return False


# --------------------------------------------------------------------------
class BitLockerVhdxBackend(SecureStorageBackend):
    """A BitLocker-protected NTFS volume inside a data VHDX.

    Every privileged step runs in :mod:`sa_privhelper`'s elevated helper; the
    unlock secret crosses on the pipe and is zeroized on both sides. Nothing
    in this class ever builds a command line containing a secret.
    """

    name = "bitlocker-vhdx"
    requires_elevation = True

    def __init__(self, helper=None, script_dir=None):
        self._helper = helper
        self._script_dir = script_dir or os.path.dirname(os.path.abspath(__file__))
        self._lock = threading.RLock()
        #: True while this backend holds a lease on the elevated helper --
        #: that is, from the moment the volume is attached until it is
        #: detached again. Outside that window the helper is free to reach
        #: its idle timeout and exit; the next unlock starts it silently.
        self._holding = False

    def _channel(self):
        with self._lock:
            if self._helper is None:
                self._helper = sa_privhelper.PrivilegedHelper(script_dir=self._script_dir)
            if not self._helper.running:
                self._helper.ensure()
            return self._helper

    def _hold(self):
        """Keep the helper for as long as the volume is attached."""
        with self._lock:
            helper = self._channel()
            if not self._holding:
                helper.acquire()
                self._holding = True
            return helper

    def _release_if_detached(self, profile):
        """Drop the lease only once the helper itself reports nothing attached.

        Lease lifetime follows physical attachment, not RPC completion
        (SRC-027 W2-006): after a failed or unverifiable detach the process
        that attached the disk must stay reachable, so the lease is kept.
        """
        try:
            detached = self.state(profile) in (DETACHED, MISSING)
        except Exception:
            detached = False
        if detached:
            self._unhold()
        return detached

    @property
    def holding(self):
        return self._holding

    def _unhold(self):
        with self._lock:
            if self._holding and self._helper is not None:
                try:
                    self._helper.release()
                except Exception:
                    pass
            self._holding = False

    def start(self):
        self._channel()

    def stop(self):
        with self._lock:
            if self._holding:
                # Something may still be attached: closing the helper now
                # would throw away the only process able to detach it.
                return
            if self._helper is not None:
                try:
                    self._helper.close()
                except Exception:
                    pass

    def _call(self, op, fallback_category, **args):
        try:
            return self._channel().call(op, **args)
        except sa_privhelper.HelperError as exc:
            raise StorageError(str(exc), getattr(exc, "category", fallback_category))

    def state(self, profile):
        if not os.path.isfile(profile.container):
            return MISSING
        result = self._call("state", "storage_mount_failed",
                            container=profile.container,
                            mount_path=profile.mount_path)
        value = result.get("state")
        if value not in (DETACHED, ATTACHED_LOCKED, MOUNTED, MISSING):
            raise StorageError("storage helper reported an unknown state")
        return value

    def mounted_at(self, profile):
        if not os.path.isfile(profile.container):
            return None
        result = self._call("state", "storage_mount_failed",
                            container=profile.container,
                            mount_path=profile.mount_path)
        if result.get("state") != MOUNTED:
            return None
        return result.get("mounted_at") or None

    def create(self, profile, volume_secret):
        if os.path.isfile(profile.container):
            raise StorageError("container already exists: %s" % profile.container,
                               "storage_create_failed")
        # Asked BEFORE anything is created. A directory mount needs an empty
        # directory, and discovering that from Add-PartitionAccessPath would
        # mean a built, attached, formatted and encrypted image to unwind.
        self.assert_mount_target(profile, "container mount target")
        directory = ntpath.dirname(profile.container)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._hold()
        return self._call("create", "storage_create_failed",
                          container=profile.container,
                          mount_path=profile.mount_path,
                          size_gb=profile.size_gb,
                          label=profile.filesystem_label,
                          secret_b64=sa_privhelper.encode_secret(volume_secret))

    def unlock_and_mount(self, profile, volume_secret):
        # The lease is taken BEFORE the unlock: from here until the volume is
        # detached again the helper must stay reachable, because the process
        # that attached the disk is the one that has to detach it.
        self._hold()
        try:
            return self._call("unlock_mount", "storage_mount_failed",
                              container=profile.container,
                              mount_path=profile.mount_path,
                              secret_b64=sa_privhelper.encode_secret(volume_secret))
        except Exception:
            # A failed unlock can still leave the disk attached.
            self._release_if_detached(profile)
            raise

    def unmount(self, profile):
        # A raising call keeps the lease: the volume may still be attached.
        result = self._call("unmount", "storage_unmount_failed",
                            container=profile.container,
                            mount_path=profile.mount_path)
        # Verified detached: nothing privileged is outstanding, so the helper
        # may idle out. A later unlock starts it again through Task Scheduler,
        # silently -- no consent dialog, no long-lived elevated process.
        if not self._release_if_detached(profile):
            raise StorageError("helper reported unmount but the volume is still "
                               "attached", "storage_unmount_failed")
        return result

    def destroy(self, profile):
        try:
            self.unmount(profile)
        except StorageError:
            pass
        if not os.path.isfile(profile.container):
            return True
        try:
            os.remove(profile.container)
        except OSError:
            return False
        return True


# --------------------------------------------------------------------------
class FakeStorageBackend(SecureStorageBackend):
    """Deterministic in-memory backend for lifecycle tests.

    Refused by :mod:`sa_config` unless test providers are explicitly enabled.
    Every failure the real backend can produce is injectable here, so the
    broker's error paths are exercised without a disk image.
    """

    name = "fake-memory"
    requires_elevation = False

    def __init__(self, root=None, exists=False):
        self.root = root
        self._state = {}
        self._secret = {}
        self._exists = {}
        # Paths this backend has attached at least once. A real volume leaves
        # its mount directory empty when it detaches, so remounting there is
        # fine; a plain directory in a test does not, so the fake remembers
        # which directories are its own instead of re-checking emptiness and
        # tripping over the copy it just made.
        self._attached_paths = {}
        self._mounted_at = {}
        self.fail_create = None
        self.fail_unlock = None
        self.fail_mount = None
        self.fail_unmount = None
        self.busy_on_unmount = False
        self.calls = []
        self.started = 0
        self.stopped = 0
        self._default_exists = exists

    # -- helpers ----------------------------------------------------------
    def _key(self, profile):
        return profile.id

    def seed(self, profile, secret_bytes, state=DETACHED):
        self._exists[self._key(profile)] = True
        self._secret[self._key(profile)] = bytes(secret_bytes)
        self._state[self._key(profile)] = state

    def force_state(self, profile, state):
        self._state[self._key(profile)] = state

    def _claim_mount_path(self, profile, what):
        """Empty-target check for a path this backend does not already own."""
        key = self._key(profile)
        owned = self._attached_paths.setdefault(key, set())
        target = str(profile.mount_path).rstrip("\\/").lower()
        if target in owned:
            return
        self.assert_mount_target(profile, what)
        owned.add(target)

    # -- interface --------------------------------------------------------
    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def state(self, profile):
        key = self._key(profile)
        if not self._exists.get(key, self._default_exists):
            return MISSING
        return self._state.get(key, DETACHED)

    def create(self, profile, volume_secret):
        self.calls.append(("create", profile.id))
        if self.fail_create:
            raise StorageError(self.fail_create, "storage_create_failed")
        key = self._key(profile)
        if self._exists.get(key):
            raise StorageError("container already exists", "storage_create_failed")
        # Same precondition as the real backend, so the enrollment flow is
        # proven against a non-empty mount target without a disk image.
        self._claim_mount_path(profile, "container mount target")
        self._exists[key] = True
        self._secret[key] = volume_secret.bytes()
        self._state[key] = MOUNTED
        self._mounted_at[key] = profile.mount_path
        os.makedirs(profile.mount_path, exist_ok=True)
        return {"container": profile.container, "mount_path": profile.mount_path,
                "recovery_password": "000000-111111-222222-333333-444444-555555-666666-777777"}

    def unlock_and_mount(self, profile, volume_secret):
        self.calls.append(("unlock_and_mount", profile.id))
        if self.fail_unlock:
            raise StorageError(self.fail_unlock, "storage_unlock_failed")
        key = self._key(profile)
        if not self._exists.get(key):
            raise StorageError("container is missing", "storage_mount_failed")
        expected = self._secret.get(key)
        if expected is not None and volume_secret.bytes() != expected:
            raise StorageError("volume secret rejected", "storage_unlock_failed")
        if self.fail_mount:
            raise StorageError(self.fail_mount, "storage_mount_failed")
        self._claim_mount_path(profile, "volume mount target")
        self._state[key] = MOUNTED
        self._mounted_at[key] = profile.mount_path
        os.makedirs(profile.mount_path, exist_ok=True)
        return {"container": profile.container, "mount_path": profile.mount_path,
                "state": MOUNTED}

    def unmount(self, profile):
        self.calls.append(("unmount", profile.id))
        if self.busy_on_unmount:
            raise StorageBusyError("a handle is still open on the protected volume")
        if self.fail_unmount:
            raise StorageError(self.fail_unmount, "storage_unmount_failed")
        self._state[self._key(profile)] = DETACHED
        self._mounted_at.pop(self._key(profile), None)
        return {"container": profile.container, "state": DETACHED}

    def mounted_at(self, profile):
        key = self._key(profile)
        if self.state(profile) != MOUNTED:
            return None
        return self._mounted_at.get(key)

    def destroy(self, profile):
        self.calls.append(("destroy", profile.id))
        key = self._key(profile)
        try:
            self.unmount(profile)
        except StorageError:
            pass
        self._exists.pop(key, None)
        self._secret.pop(key, None)
        self._state.pop(key, None)
        self._attached_paths.pop(key, None)
        self._default_exists = False
        try:
            if os.path.isfile(profile.container):
                os.remove(profile.container)
        except OSError:
            return False
        return True

    def mount_is_live(self, profile):
        return self._state.get(self._key(profile)) == MOUNTED


BACKEND_FACTORIES = {
    BitLockerVhdxBackend.name: lambda profile: BitLockerVhdxBackend(),
}


def build_backend(profile, test_backends=None):
    if test_backends and profile.backend in test_backends:
        return test_backends[profile.backend]
    factory = BACKEND_FACTORIES.get(profile.backend)
    if factory is None:
        raise StorageError("no implementation for backend %r" % (profile.backend,))
    return factory(profile)
