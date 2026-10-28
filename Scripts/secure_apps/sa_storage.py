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

    def create(self, profile, volume_secret, portable_password=None):
        """Create the container. Returns a dict with ``recovery_password``.

        The recovery material is returned exactly once, to the caller, for a
        one-time display. It is never written next to the container.
        """
        raise NotImplementedError

    def unlock_and_mount(self, profile, volume_secret=None, password=None,
                         recovery_password=None, read_only=False):
        raise NotImplementedError

    def add_portable_password(self, profile, password):
        raise NotImplementedError

    def change_portable_password(self, profile, new_password):
        raise NotImplementedError

    def remove_portable_password(self, profile):
        raise NotImplementedError

    def list_protectors(self, profile):
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

    def create(self, profile, volume_secret, portable_password=None):
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
        args = {
            "container": profile.container,
            "mount_path": profile.mount_path,
            "size_gb": profile.size_gb,
            "label": profile.filesystem_label,
            "secret_b64": sa_privhelper.encode_secret(volume_secret),
        }
        if portable_password:
            raw_pw = portable_password.encode("utf-8") if isinstance(portable_password, str) else portable_password
            args["portable_password_b64"] = sa_privhelper.encode_secret(raw_pw)
        return self._call("create", "storage_create_failed", **args)

    def unlock_and_mount(self, profile, volume_secret=None, password=None,
                         recovery_password=None, read_only=False):
        # The lease is taken BEFORE the unlock: from here until the volume is
        # detached again the helper must stay reachable, because the process
        # that attached the disk is the one that has to detach it.
        self._hold()
        try:
            args = {
                "container": profile.container,
                "mount_path": profile.mount_path,
                "read_only": bool(read_only),
            }
            if password:
                raw_pw = password.encode("utf-8") if isinstance(password, str) else password
                args["password_b64"] = sa_privhelper.encode_secret(raw_pw)
            elif recovery_password:
                args["recovery_password"] = str(recovery_password).strip()
            elif volume_secret is not None:
                args["secret_b64"] = sa_privhelper.encode_secret(volume_secret)
            else:
                raise StorageError("no unlock secret, password, or recovery key provided",
                                   "storage_unlock_failed")
            return self._call("unlock_mount", "storage_mount_failed", **args)
        except Exception:
            # A failed unlock can still leave the disk attached.
            self._release_if_detached(profile)
            raise

    def add_portable_password(self, profile, password):
        self._hold()
        try:
            raw_pw = password.encode("utf-8") if isinstance(password, str) else password
            pw_b64 = sa_privhelper.encode_secret(raw_pw)
            return self._call("portable_password_add", "storage_mount_failed",
                              container=profile.container, password_b64=pw_b64,
                              mount_path=profile.mount_path or "")
        finally:
            self._release_if_detached(profile)

    def change_portable_password(self, profile, new_password, expected_portable_id=None,
                                 old_password=None, keep_escrow=False):
        self._hold()
        try:
            raw_pw = new_password.encode("utf-8") if isinstance(new_password, str) else new_password
            pw_b64 = sa_privhelper.encode_secret(raw_pw)
            kwargs = {
                "container": profile.container,
                "new_password_b64": pw_b64,
                "mount_path": profile.mount_path or "",
                "keep_escrow": bool(keep_escrow),
            }
            if expected_portable_id:
                kwargs["expected_portable_id"] = str(expected_portable_id)
            if old_password:
                raw_old = old_password.encode("utf-8") if isinstance(old_password, str) else old_password
                kwargs["old_password_b64"] = sa_privhelper.encode_secret(raw_old)
            return self._call("portable_password_change", "storage_mount_failed", **kwargs)
        finally:
            self._release_if_detached(profile)

    def verify_password_unlock(self, profile, password):
        self._hold()
        try:
            raw_pw = password.encode("utf-8") if isinstance(password, str) else password
            pw_b64 = sa_privhelper.encode_secret(raw_pw)
            res = self._call("verify_password", "storage_unlock_failed",
                             container=profile.container,
                             password_b64=pw_b64,
                             mount_path=profile.mount_path or "")
            return bool(res.get("verified") or res.get("ok"))
        finally:
            self._release_if_detached(profile)

    def remove_portable_password(self, profile, expected_portable_id=None):
        self._hold()
        try:
            kwargs = {
                "container": profile.container,
                "mount_path": profile.mount_path or ""
            }
            if expected_portable_id:
                kwargs["expected_portable_id"] = str(expected_portable_id)
            return self._call("portable_password_remove", "storage_mount_failed", **kwargs)
        finally:
            self._release_if_detached(profile)

    def remove_protector(self, profile, protector_id):
        self._hold()
        try:
            return self._call("remove_protector", "storage_mount_failed",
                              container=profile.container,
                              protector_id=str(protector_id))
        finally:
            self._release_if_detached(profile)

    def upgrade_legacy_vault(self, profile, portable_password=None, old_daily_password=None, keep_old_daily=False):
        self._hold()
        try:
            kwargs = {
                "container": profile.container,
                "keep_old_daily": bool(keep_old_daily),
            }
            if portable_password:
                raw_pw = portable_password.encode("utf-8") if isinstance(portable_password, str) else portable_password
                kwargs["portable_password_b64"] = sa_privhelper.encode_secret(raw_pw)
            if old_daily_password:
                raw_old = old_daily_password.encode("utf-8") if isinstance(old_daily_password, str) else old_daily_password
                kwargs["old_daily_password_b64"] = sa_privhelper.encode_secret(raw_old)
            return self._call("upgrade_legacy_vault", "storage_mount_failed", **kwargs)
        finally:
            self._release_if_detached(profile)

    def list_protectors(self, profile):
        self._hold()
        try:
            return self._call("protectors_list", "storage_mount_failed",
                              container=profile.container)
        finally:
            self._release_if_detached(profile)

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
        self._passwords = {}
        self._recovery_passwords = {}
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

    def create(self, profile, volume_secret, portable_password=None):
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
        if portable_password:
            self._passwords[key] = portable_password
        self._recovery_passwords[key] = "000000-111111-222222-333333-444444-555555-666666-777777"
        self._state[key] = MOUNTED
        self._mounted_at[key] = profile.mount_path
        os.makedirs(profile.mount_path, exist_ok=True)
        ret = {
            "container": profile.container,
            "mount_path": profile.mount_path,
            "recovery_password": self._recovery_passwords[key],
            "break_glass_protector_id": "{fake-rec-id}",
            "daily_protector_id": "{fake-daily-id}",
        }
        if portable_password:
            ret["daily_recovery_password"] = "888888-888888-888888-888888-888888-888888-888888-888888"
            ret["has_portable_password"] = True
            ret["portable_password_id"] = "{fake-pw-id}"
        return ret

    def unlock_and_mount(self, profile, volume_secret=None, password=None,
                         recovery_password=None, read_only=False):
        self.calls.append(("unlock_and_mount", profile.id))
        if self.fail_unlock:
            raise StorageError(self.fail_unlock, "storage_unlock_failed")
        key = self._key(profile)
        if not self._exists.get(key):
            raise StorageError("container is missing", "storage_mount_failed")

        if password:
            expected_pw = self._passwords.get(key)
            if expected_pw is None or (password != expected_pw):
                raise StorageError("master password rejected", "storage_unlock_failed")
        elif recovery_password:
            expected_rec = self._recovery_passwords.get(key)
            if expected_rec is None or (str(recovery_password).strip() != expected_rec):
                raise StorageError("recovery password rejected", "storage_unlock_failed")
        elif volume_secret is not None:
            expected = self._secret.get(key)
            if expected is not None and volume_secret.bytes() != expected:
                raise StorageError("volume secret rejected", "storage_unlock_failed")
        else:
            raise StorageError("no unlock secret provided", "storage_unlock_failed")

        if self.fail_mount:
            raise StorageError(self.fail_mount, "storage_mount_failed")
        self._claim_mount_path(profile, "volume mount target")
        self._state[key] = MOUNTED
        self._mounted_at[key] = profile.mount_path
        os.makedirs(profile.mount_path, exist_ok=True)
        return {"container": profile.container, "mount_path": profile.mount_path,
                "state": MOUNTED, "read_only": bool(read_only)}

    def add_portable_password(self, profile, password):
        self.calls.append(("add_portable_password", profile.id))
        key = self._key(profile)
        if self._passwords.get(key):
            raise StorageError("password protector already exists", "storage_mount_failed")
        self._passwords[key] = password
        if not hasattr(self, "_current_pw_id"):
            self._current_pw_id = {}
        self._current_pw_id[key] = "{fake-pw-id}"
        return {"ok": True, "container": profile.container, "protector_id": "{fake-pw-id}", "protector_type": "Password"}

    def change_portable_password(self, profile, new_password, expected_portable_id=None,
                                 old_password=None, keep_escrow=False):
        self.calls.append(("change_portable_password", profile.id))
        key = self._key(profile)
        if not self._passwords.get(key):
            raise StorageError("no password protector to change", "storage_mount_failed")
        current_id = getattr(self, "_current_pw_id", {}).get(key, "{fake-pw-id}")
        if expected_portable_id and expected_portable_id != current_id:
            raise StorageError(f"expected_portable_id mismatch: {expected_portable_id} != {current_id}", "storage_mount_failed")

        escrow_id = "{fake-escrow-id}"
        escrow_key = "999999-999999-999999-999999-999999-999999-999999-999999"
        if not hasattr(self, "_escrows"):
            self._escrows = {}
        if keep_escrow:
            self._escrows[key] = {"id": escrow_id, "key": escrow_key}

        new_id = "{fake-pw-id-2}"
        if not hasattr(self, "_current_pw_id"):
            self._current_pw_id = {}
        self._current_pw_id[key] = new_id
        self._passwords[key] = new_password
        return {
            "ok": True,
            "container": profile.container,
            "protector_id": new_id,
            "protector_type": "Password",
            "escrow_id": escrow_id,
            "escrow_key": escrow_key,
        }

    def verify_password_unlock(self, profile, password):
        self.calls.append(("verify_password_unlock", profile.id))
        if self.fail_unlock:
            raise StorageError(self.fail_unlock, "storage_unlock_failed")
        key = self._key(profile)
        expected = self._passwords.get(key)
        return bool(expected and expected == password)

    def remove_portable_password(self, profile, expected_portable_id=None):
        self.calls.append(("remove_portable_password", profile.id))
        key = self._key(profile)
        self._passwords.pop(key, None)
        if hasattr(self, "_current_pw_id"):
            self._current_pw_id.pop(key, None)
        return {"ok": True, "container": profile.container, "state": "removed"}

    def remove_protector(self, profile, protector_id):
        self.calls.append(("remove_protector", profile.id, protector_id))
        key = self._key(profile)
        if hasattr(self, "_escrows") and key in self._escrows:
            if self._escrows[key]["id"] == protector_id:
                del self._escrows[key]
        if hasattr(self, "_daily_protectors") and key in self._daily_protectors:
            if str(self._daily_protectors[key]).lower() == str(protector_id).lower():
                del self._daily_protectors[key]
        current_pw_id = getattr(self, "_current_pw_id", {}).get(key, "{fake-pw-id}")
        if str(protector_id).lower() == str(current_pw_id).lower():
            self._passwords.pop(key, None)
            if hasattr(self, "_current_pw_id"):
                self._current_pw_id.pop(key, None)
        return {"ok": True, "removed_id": protector_id}

    def upgrade_legacy_vault(self, profile, portable_password=None, old_daily_password=None, keep_old_daily=False):
        self.calls.append(("upgrade_legacy_vault", profile.id))
        key = self._key(profile)
        if not hasattr(self, "_daily_protectors"):
            self._daily_protectors = {}
        self._daily_protectors[key] = "{fake-daily-id}"
        if portable_password:
            self._passwords[key] = portable_password
            if not hasattr(self, "_current_pw_id"):
                self._current_pw_id = {}
            self._current_pw_id[key] = "{fake-pw-id}"
        return {
            "ok": True,
            "container": profile.container,
            "storage_layout_version": 2,
            "daily_recovery_password": "888888-888888-888888-888888-888888-888888-888888-888888",
            "daily_protector_id": "{fake-daily-id}",
            "old_daily_protector_id": "{fake-old-daily-pw}",
            "portable_protector_id": "{fake-pw-id}" if portable_password else None,
            "break_glass_protector_id": "{fake-rec-id}",
        }

    def list_protectors(self, profile):
        self.calls.append(("list_protectors", profile.id))
        key = self._key(profile)
        protectors = []
        if key in self._passwords:
            port_id = getattr(self, "_current_pw_id", {}).get(key, "{fake-pw-id}")
            protectors.append({"id": port_id, "type": "Password"})
        if key in self._recovery_passwords:
            protectors.append({"id": "{fake-rec-id}", "type": "RecoveryPassword"})
        if hasattr(self, "_daily_protectors") and key in self._daily_protectors:
            protectors.append({"id": self._daily_protectors[key], "type": "RecoveryPassword"})
        else:
            import sa_layout
            layout = sa_layout.load_layout(profile)
            if layout and layout.get("daily_protector") and layout["daily_protector"].get("id"):
                protectors.append({"id": layout["daily_protector"]["id"], "type": "RecoveryPassword"})
        if getattr(self, "_escrows", {}).get(key):
            protectors.append({"id": self._escrows[key]["id"], "type": "RecoveryPassword"})
        return {"container": profile.container, "lock_status": "Unlocked" if self._state.get(key) == MOUNTED else "Locked", "protectors": protectors}

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
