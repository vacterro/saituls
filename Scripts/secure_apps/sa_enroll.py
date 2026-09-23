"""Enrollment and recovery for SAITULS Secure Apps.

Two separate operations, and the difference matters:

:func:`begin_enrollment` / :class:`PendingEnrollment`
    First run, as an explicit state machine::

        ENROLL_PREPARE
          -> create the encrypted container at a STAGING mount
          -> emit the recovery material (once)
        ENROLL_AWAITING_RECOVERY_ACK
          -> ENROLL_RECOVERY_ACCEPT  -> enroll key -> unmount -> ENROLLMENT_READY
          -> ENROLL_RECOVERY_DECLINE -> zeroize -> roll the container back

    The brand-new container is mounted at
    ``<container dir>\_enrollment_mount`` and **never** at the profile's real
    mount path: on a first run that path still holds the user's plaintext
    data, and Windows attaches a volume only to an empty directory. Creating
    there would either fail or demand that the plaintext be moved before
    anything had been verified -- the circular dependency between enrollment
    and migration that this design removes.

    Between PREPARE and the answer the volume secret lives in a zeroizing
    :class:`sa_crypto.SecretBuffer` inside :class:`PendingEnrollment`, with a
    bounded timeout, explicit cancellation, and zeroization plus rollback on
    every exit that is not an acceptance. Nothing writes the recovery
    password to disk -- least of all next to the container it opens.

:func:`create_and_enroll`
    The same flow for a caller that owns its thread and may block on a
    console prompt (the CLI, the interactive hardware script). A GUI must not
    block its broker thread waiting for a modal answer, so it drives the
    state machine directly.

:func:`enroll_additional`
    Adds a second (third, ...) security key. The existing key authenticates
    once, the volume secret is unwrapped in memory, and a second wrapped copy
    is written for the new credential. The container is **not** re-encrypted:
    a backup key costs one touch of each key, not a full rewrite of the
    vault. This is the mechanism that makes "losing one FIDO2 key destroys
    the vault" untrue, and it is why the key hierarchy wraps a volume secret
    instead of deriving the volume key from the authenticator directly.
"""
import os
import threading
import time
import uuid

import sa_auth
import sa_crypto
import sa_paths
import sa_storage


class EnrollmentRefused(Exception):
    category = "auth_capability"

    def __init__(self, message, category=None):
        Exception.__init__(self, message)
        if category:
            self.category = category


class EnrollmentResult(object):
    __slots__ = ("ok", "profile_id", "credential_id_hash", "recovery_shown",
                 "reason", "error_category", "container_created", "state",
                 "staging_mount")

    def __init__(self, ok, profile_id, credential_id_hash="", recovery_shown=False,
                 reason="", error_category="none", container_created=False,
                 state="", staging_mount=""):
        self.state = state
        self.staging_mount = staging_mount
        self.ok = ok
        self.profile_id = profile_id
        self.credential_id_hash = credential_id_hash
        self.recovery_shown = recovery_shown
        self.reason = reason
        self.error_category = error_category
        self.container_created = container_created

    def to_dict(self):
        return {name: getattr(self, name) for name in self.__slots__}


def capabilities(broker, profile_id):
    """What the connected authenticator can actually do, before anything else.

    Enrollment calls this first so a presence-only key is rejected with a
    capability message instead of silently producing a vault that is not
    cryptographically protected.
    """
    profile = broker.registry.get(profile_id)
    provider = broker.provider(profile)
    caps = provider.capabilities()
    return caps.to_dict()


def _require_hmac_secret(broker, profile, provider):
    caps = provider.capabilities()
    if not caps.available:
        raise EnrollmentRefused(
            "no authenticator is available for provider %s: %s"
            % (provider.name, caps.detail or "not detected"), "auth_unavailable")
    if not caps.hmac_secret:
        raise EnrollmentRefused(
            "the connected authenticator does not support the CTAP2 "
            "hmac-secret extension. Encrypted-vault enrollment cannot "
            "continue: without it the key cannot produce the material that "
            "encrypts the vault, and SAITULS will not downgrade an encrypted "
            "vault to a presence-only check.", "auth_capability")


# --------------------------------------------------------------------------
# first-run enrollment state machine
# --------------------------------------------------------------------------
# Enrollment is not one function call, because one of its steps is a human
# reading a BitLocker recovery key off a screen. Modelling that as a blocking
# callback inside the broker thread is what produced the original deadlock:
# the answer arrived as a queued slot on the very object whose thread was
# asleep waiting for it. The states below are explicit instead, and the GUI
# drives them from its own thread.
ENROLL_PREPARE = "ENROLL_PREPARE"
ENROLL_AWAITING_RECOVERY_ACK = "ENROLL_AWAITING_RECOVERY_ACK"
ENROLL_RECOVERY_ACCEPT = "ENROLL_RECOVERY_ACCEPT"
ENROLL_RECOVERY_DECLINE = "ENROLL_RECOVERY_DECLINE"
ENROLLMENT_READY = "ENROLLMENT_READY"
ENROLL_ROLLED_BACK = "ENROLL_ROLLED_BACK"
ENROLL_FAILED = "ENROLL_FAILED"
#: The decision is final but its storage cleanup is not: an accepted
#: enrollment whose staging volume is still attached, or a declined one whose
#: container could not be removed. Never a success; retry_cleanup() resolves
#: it (SRC-027 W2-002).
ENROLL_CLEANUP_REQUIRED = "ENROLL_CLEANUP_REQUIRED"

ENROLL_STATES = (ENROLL_PREPARE, ENROLL_AWAITING_RECOVERY_ACK,
                 ENROLL_RECOVERY_ACCEPT, ENROLL_RECOVERY_DECLINE,
                 ENROLLMENT_READY, ENROLL_ROLLED_BACK, ENROLL_FAILED,
                 ENROLL_CLEANUP_REQUIRED)

#: How long a shown-but-unanswered recovery dialog may keep a brand-new
#: container and its volume secret alive. On expiry the secret is zeroized
#: and the container is rolled back: an unanswered dialog is not consent.
RECOVERY_ACK_TIMEOUT_SECONDS = 600

CLEANUP_DETACH_REASON = ("credential enrolled but the staging volume is still "
                         "attached; retry the cleanup before using this vault")


def enrollment_mount(profile):
    """The transient mount a first-run enrollment uses.

    **Never** the profile's real mount path. On a first run that path still
    holds the user's plaintext data, and Windows attaches a volume only to an
    empty directory -- so creating there would either fail outright or demand
    that the plaintext be moved before a single byte had been verified. That
    circular dependency (enrollment wants the path, migration wants the
    container) is the reason this function exists.
    """
    return sa_paths.enrollment_mount_path(profile.container)


class PendingEnrollment(object):
    """A created-but-unacknowledged container, and the secret that opens it.

    Holds the volume secret in a zeroizing :class:`sa_crypto.SecretBuffer`
    and nothing else that matters: the recovery password is handed to the
    caller for its one-time display and is never stored here. Every exit --
    accept, decline, timeout, cancel -- zeroizes, and every exit except
    accept rolls the container back.
    """

    def __init__(self, broker, profile, staged_profile, backend, provider,
                 volume_secret, timeout_seconds=RECOVERY_ACK_TIMEOUT_SECONDS,
                 clock=time.monotonic):
        self.broker = broker
        self.profile = profile
        self.staged_profile = staged_profile
        self.backend = backend
        self.provider = provider
        self._secret = volume_secret
        self._clock = clock
        self._deadline = clock() + float(timeout_seconds)
        self._lock = threading.RLock()
        self.state = ENROLL_AWAITING_RECOVERY_ACK
        #: "detach" (accepted, staging still attached) or "rollback"
        #: (declined, container still present) while cleanup is outstanding.
        self._cleanup = None
        self._credential_hash = None

    # -- introspection ----------------------------------------------------
    @property
    def profile_id(self):
        return self.profile.id

    @property
    def resolved(self):
        return self.state not in (ENROLL_PREPARE, ENROLL_AWAITING_RECOVERY_ACK)

    @property
    def cleanup_required(self):
        return self.state == ENROLL_CLEANUP_REQUIRED

    @property
    def expired(self):
        with self._lock:
            return (not self.resolved) and self._clock() >= self._deadline

    def seconds_remaining(self):
        with self._lock:
            return max(0.0, self._deadline - self._clock())

    # -- teardown ---------------------------------------------------------
    def _zeroize(self):
        secret, self._secret = self._secret, None
        if secret is not None:
            secret.zeroize()

    def _rollback(self, transition, category):
        removed = _rollback_container(self.broker, self.staged_profile,
                                      self.backend)
        self.broker.audit.write(transition, profile_id=self.profile_id,
                                result="refused", error_category=category)
        return removed

    # -- transitions ------------------------------------------------------
    def accept(self):
        """The user confirmed the recovery material is stored offline."""
        with self._lock:
            if self.resolved:
                raise EnrollmentRefused(
                    "this enrollment already finished as %s" % self.state,
                    "config_invalid")
            if self.expired:
                return self.decline(timed_out=True)
            try:
                credential = self.provider.create_credential(
                    self.profile.credential_profile, require_hmac_secret=True)
                enrollment = sa_auth.wrap_volume_secret_for(
                    self.profile, self.provider, credential, self._secret,
                    container_id=self.profile.container_id)
                self.broker.credentials.add(self.profile_id,
                                            self.profile.container_id,
                                            enrollment)
            except Exception as exc:
                self.state = ENROLL_FAILED
                category = getattr(exc, "category", "auth_failed")
                self._rollback("enroll_rollback",
                               category if category in _AUDIT_CATEGORIES
                               else "auth_failed")
                self.broker.audit.write(
                    "enroll", profile_id=self.profile_id, result="failed",
                    error_category=(category if category in _AUDIT_CATEGORIES
                                    else "auth_failed"))
                raise
            finally:
                self._zeroize()
            # The vault is enrolled; the staging mount must now be detached.
            # Detach is part of the transaction's final verdict: if it fails,
            # the enrollment is valid but cleanup is required.
            self._credential_hash = sa_crypto.credential_id_hash(
                enrollment.credential_id)
            detached = _release_staging(self.broker, self.staged_profile,
                                        self.backend)
            if not detached:
                self.state = ENROLL_CLEANUP_REQUIRED
                self._cleanup = "detach"
                self.broker.audit.write("enroll_staging_release",
                                        profile_id=self.profile_id,
                                        result="failed",
                                        error_category="storage_unmount_failed",
                                        mount_path=self.staged_profile.mount_path)
                self.broker.audit.write("enroll",
                                        profile_id=self.profile_id,
                                        result="failed",
                                        error_category="storage_unmount_failed")
                return EnrollmentResult(
                    False, self.profile_id,
                    credential_id_hash=sa_crypto.credential_id_hash(
                        enrollment.credential_id),
                    recovery_shown=True, container_created=True,
                    state=ENROLL_CLEANUP_REQUIRED,
                    staging_mount=self.staged_profile.mount_path,
                    reason=CLEANUP_DETACH_REASON,
                    error_category="storage_unmount_failed")
            return self._ready()

    def _ready(self):
        """ENROLLMENT_READY -- reached only once staging is verified detached."""
        self.state = ENROLLMENT_READY
        self._cleanup = None
        credential_hash = self._credential_hash
        self.broker.audit.write(
            "enroll", profile_id=self.profile_id, result="ok",
            auth_provider=self.provider.name,
            backend=self.profile.backend,
            container_id=self.profile.container_id,
            credential_id_hash=credential_hash)
        self.broker.audit.write(ENROLLMENT_READY.lower(),
                                profile_id=self.profile_id,
                                result="ok",
                                mount_path=self.staged_profile.mount_path)
        return EnrollmentResult(
            True, self.profile_id, credential_id_hash=credential_hash,
            recovery_shown=True, container_created=True,
            state=ENROLLMENT_READY,
            staging_mount=self.staged_profile.mount_path,
            reason="container created and first key enrolled; the volume "
                   "is locked and detached, and the profile's real mount "
                   "path was never touched")

    def decline(self, timed_out=False, cancelled=False, reason=None):
        """No acknowledgement: zeroize, roll the empty container back."""
        with self._lock:
            if self.resolved:
                raise EnrollmentRefused(
                    "this enrollment already finished as %s" % self.state,
                    "config_invalid")
            self._zeroize()
            self.state = ENROLL_RECOVERY_DECLINE
            if timed_out:
                transition = "enroll_recovery_timeout"
                default = ("the BitLocker recovery material was shown but not "
                           "acknowledged within %d seconds; the empty "
                           "container was removed rather than left protected "
                           "by a single key"
                           % int(RECOVERY_ACK_TIMEOUT_SECONDS))
            elif cancelled:
                transition = "enroll_cancelled"
                default = ("enrollment was cancelled before the recovery "
                           "material was acknowledged; the empty container "
                           "was removed")
            else:
                transition = "enroll_recovery_declined"
                default = ("the BitLocker recovery material was not "
                           "acknowledged; the empty container was removed "
                           "rather than left protected by a single key")
            removed = self._rollback(transition, "config_invalid")
            if removed:
                self.state = ENROLL_ROLLED_BACK
                return EnrollmentResult(
                    False, self.profile_id, recovery_shown=True,
                    state=self.state,
                    staging_mount=self.staged_profile.mount_path,
                    reason=reason or default,
                    error_category="config_invalid")
            self.state = ENROLL_CLEANUP_REQUIRED
            self._cleanup = "rollback"
            return self._rollback_pending_result()

    def _rollback_pending_result(self):
        return EnrollmentResult(
            False, self.profile_id, recovery_shown=True,
            container_created=True, state=ENROLL_CLEANUP_REQUIRED,
            staging_mount=self.staged_profile.mount_path,
            reason=("the unacknowledged container could not be removed; "
                    "retry the cleanup (container: %s)"
                    % self.staged_profile.container),
            error_category="storage_create_failed")

    def cancel(self, reason=None):
        """Explicit cancellation -- GUI shutdown, or a caller that gave up."""
        return self.decline(cancelled=True, reason=reason)

    def retry_cleanup(self):
        """Finish the storage cleanup a resolved enrollment still owes.

        Accepted: detach the staging volume, then -- and only then --
        ENROLLMENT_READY. Declined: remove the container, then
        ENROLL_ROLLED_BACK. A repeated failure stays ENROLL_CLEANUP_REQUIRED.
        """
        with self._lock:
            if not self.cleanup_required:
                raise EnrollmentRefused(
                    "no cleanup outstanding (state %s)" % self.state,
                    "config_invalid")
            if self._cleanup == "detach":
                if _release_staging(self.broker, self.staged_profile,
                                    self.backend):
                    return self._ready()
                return EnrollmentResult(
                    False, self.profile_id,
                    credential_id_hash=self._credential_hash,
                    recovery_shown=True, container_created=True,
                    state=ENROLL_CLEANUP_REQUIRED,
                    staging_mount=self.staged_profile.mount_path,
                    reason=CLEANUP_DETACH_REASON,
                    error_category="storage_unmount_failed")
            if self._rollback("enroll_rollback_retry", "config_invalid"):
                self.state = ENROLL_ROLLED_BACK
                self._cleanup = None
                return EnrollmentResult(
                    False, self.profile_id, recovery_shown=True,
                    state=ENROLL_ROLLED_BACK,
                    staging_mount=self.staged_profile.mount_path,
                    reason="the unacknowledged container was removed",
                    error_category="config_invalid")
            return self._rollback_pending_result()


def begin_enrollment(broker, profile_id, size_gb=None,
                     timeout_seconds=RECOVERY_ACK_TIMEOUT_SECONDS,
                     clock=time.monotonic):
    """ENROLL_PREPARE: build the container, return the recovery material once.

    Returns ``(PendingEnrollment, recovery_password)``. The caller MUST
    resolve the pending enrollment -- :meth:`PendingEnrollment.accept`,
    :meth:`~PendingEnrollment.decline` or
    :meth:`~PendingEnrollment.cancel` -- or the volume secret stays in memory
    until the timeout fires.

    The container is created at :func:`enrollment_mount`, never at the
    profile's real mount path.
    """
    profile = broker.registry.get(profile_id)
    runtime = broker.runtime(profile_id)
    backend = broker.backend(profile)
    provider = broker.provider(profile)
    with runtime.lock:
        if broker.credentials.is_enrolled(profile_id):
            raise EnrollmentRefused(
                "profile %r is already enrolled; use 'add another key' instead"
                % profile_id, "config_invalid")
        _require_hmac_secret(broker, profile, provider)
        backend.start()
        if backend.state(profile) != sa_storage.MISSING:
            raise EnrollmentRefused(
                "a container already exists at %s; refusing to create a second "
                "one over it" % profile.container, "storage_create_failed")

        if size_gb:
            profile.size_gb = int(size_gb)
        staged_profile = profile.with_mount_path(enrollment_mount(profile))
        volume_secret = sa_crypto.new_volume_secret()
        created = False
        try:
            broker.audit.write("enroll_begin", profile_id=profile_id, result="ok",
                               auth_provider=provider.name,
                               backend=profile.backend,
                               container_id=profile.container_id,
                               mount_path=staged_profile.mount_path)
            result = backend.create(staged_profile, volume_secret)
            created = True
            pending = PendingEnrollment(broker, profile, staged_profile, backend,
                                        provider, volume_secret,
                                        timeout_seconds=timeout_seconds,
                                        clock=clock)
            broker.audit.write(ENROLL_AWAITING_RECOVERY_ACK.lower(),
                               profile_id=profile_id, result="ok",
                               mount_path=staged_profile.mount_path)
            # The recovery password leaves in the return value and is kept
            # nowhere else -- not in the pending context, not in the audit
            # log, not beside the container it opens.
            return pending, (result.get("recovery_password") or "")
        except Exception as exc:
            if created:
                _rollback_container(broker, staged_profile, backend)
            volume_secret.zeroize()
            category = getattr(exc, "category", "storage_create_failed")
            broker.audit.write("enroll", profile_id=profile_id, result="failed",
                               error_category=category if category in
                               _AUDIT_CATEGORIES else "storage_create_failed")
            raise


def create_and_enroll(broker, profile_id, acknowledge_recovery, size_gb=None,
                      timeout_seconds=RECOVERY_ACK_TIMEOUT_SECONDS):
    """Synchronous first-run enrollment, for callers that can block.

    A thin driver over the state machine above: the CLI and the interactive
    hardware script own their own thread and may wait on a console prompt.
    The GUI must not, and drives :func:`begin_enrollment` directly.

    The callback receives ``(recovery_password, profile)`` and must return
    True to confirm that the material has been stored offline. Anything else
    -- False, None, an exception -- rolls the empty container back.
    """
    pending, recovery = begin_enrollment(broker, profile_id, size_gb=size_gb,
                                         timeout_seconds=timeout_seconds)
    try:
        acknowledged = bool(acknowledge_recovery(recovery, pending.profile))
    except BaseException:
        pending.cancel("the recovery acknowledgement failed before it was "
                       "answered; the empty container was removed")
        raise
    finally:
        # The recovery password exists in this frame and nowhere else.
        recovery = None
        del recovery
    if not acknowledged:
        return pending.decline()
    return pending.accept()


def _release_staging(broker, staged_profile, backend):
    """Detach the enrollment volume and drop its transient mount directory."""
    try:
        backend.unmount(staged_profile)
    except Exception:
        broker.audit.write("enroll_staging_release", profile_id=staged_profile.id,
                           result="failed", error_category="storage_unmount_failed",
                           mount_path=staged_profile.mount_path)
        return False
    try:
        if os.path.isdir(staged_profile.mount_path):
            os.rmdir(staged_profile.mount_path)
    except OSError:
        pass
    return True


def _rollback_container(broker, profile, backend):
    """Remove a container that was created but never successfully enrolled.

    The backend owns the container, so it does the removing; this function
    only decides that it must happen and records that it did.
    """
    try:
        removed = bool(backend.destroy(profile))
    except Exception:
        removed = False
    if not removed:
        broker.audit.write("enroll_rollback", profile_id=profile.id,
                           result="failed",
                           error_category="storage_create_failed",
                           mount_path=profile.mount_path)
        return False
    try:
        if os.path.isdir(profile.mount_path):
            os.rmdir(profile.mount_path)
    except OSError:
        pass
    broker.audit.write("enroll_rollback", profile_id=profile.id, result="ok",
                       mount_path=profile.mount_path)
    return True


def enroll_additional(broker, profile_id):
    """Add another FIDO2 key to an existing vault without re-encrypting it."""
    profile = broker.registry.get(profile_id)
    runtime = broker.runtime(profile_id)
    provider = broker.provider(profile)
    with runtime.lock:
        if not broker.credentials.is_enrolled(profile_id):
            raise EnrollmentRefused(
                "profile %r has no first key yet" % profile_id, "not_enrolled")
        _require_hmac_secret(broker, profile, provider)
        broker.audit.write("enroll_additional_begin", profile_id=profile_id,
                           result="ok", auth_provider=provider.name)
        existing = broker.credentials.enrollments(profile_id)
        # Reuse the profile's hmac-secret salt: every enrolled credential then
        # lives in one CTAP allowList, so unlocking a two-key vault is still a
        # single assertion and a single touch.
        shared_salt = existing[0].hmac_salt if existing else None
        # Step 1: the EXISTING key proves ownership and hands back the secret.
        _existing_id, volume_secret = sa_auth.unwrap_volume_secret(
            profile, provider, broker.credentials)
        try:
            # Step 2: the NEW key gets its own wrapped copy of the same secret.
            credential = provider.create_credential(profile.credential_profile,
                                                    require_hmac_secret=True)
            enrollment = sa_auth.wrap_volume_secret_for(
                profile, provider, credential, volume_secret,
                container_id=broker.credentials.container_id(profile_id)
                or profile.container_id,
                hmac_salt=shared_salt)
            broker.credentials.add(profile_id,
                                   broker.credentials.container_id(profile_id)
                                   or profile.container_id, enrollment)
        finally:
            volume_secret.zeroize()
        broker.audit.write(
            "enroll_additional", profile_id=profile_id, result="ok",
            auth_provider=provider.name,
            credential_id_hash=sa_crypto.credential_id_hash(enrollment.credential_id))
        return EnrollmentResult(
            True, profile_id,
            credential_id_hash=sa_crypto.credential_id_hash(enrollment.credential_id),
            reason="additional key enrolled; the vault was not re-encrypted")


class _FilteredStore(object):
    def __init__(self, inner, enrollments, container_id):
        self._inner = inner
        self._enrollments = list(enrollments)
        self._container_id = container_id

    def enrollments(self, profile_id):
        return list(self._enrollments)

    def container_id(self, profile_id):
        return self._container_id


def remove_credential(broker, profile_id, credential_id):
    """Drop one enrolled key. The store refuses to remove the last one."""
    runtime = broker.runtime(profile_id)
    with runtime.lock:
        broker.credentials.remove(profile_id, credential_id)
        broker.audit.write("enroll_removed", profile_id=profile_id, result="ok",
                           credential_id_hash=sa_crypto.credential_id_hash(credential_id))
        return EnrollmentResult(True, profile_id,
                                credential_id_hash=sa_crypto.credential_id_hash(credential_id),
                                reason="credential removed")


def remove_credential_authenticated(broker, profile_id, credential_ident):
    """Drop one enrolled key after proving physical ownership of another.

    Refuses to remove the last enrolled credential. Removing requires
    authenticating against one of the other still-valid credentials first.
    """
    profile = broker.registry.get(profile_id)
    runtime = broker.runtime(profile_id)
    with runtime.lock:
        existing = broker.credentials.enrollments(profile_id)
        if len(existing) <= 1:
            raise sa_auth.AuthError(
                "refusing to remove the last enrolled credential for profile %r: "
                "the vault would become unopenable except through its BitLocker "
                "recovery material" % profile_id)

        target = None
        for e in existing:
            e_hash = sa_crypto.credential_id_hash(e.credential_id)
            if (e.credential_id == credential_ident
                    or e_hash == credential_ident
                    or e_hash.startswith(str(credential_ident))
                    or e.credential_profile == credential_ident):
                target = e
                break

        if not target:
            raise sa_auth.NotEnrolledError(
                "credential %r is not enrolled for profile %r"
                % (credential_ident, profile_id))

        remaining = [e for e in existing if e.credential_id != target.credential_id]
        if not remaining:
            raise sa_auth.AuthError(
                "refusing to remove the last enrolled credential for profile %r"
                % profile_id)

        container_id = broker.credentials.container_id(profile_id) or profile.container_id
        filtered = _FilteredStore(broker.credentials, remaining, container_id)
        provider = broker.provider(profile)

        # Proves the user has and can authenticate with one of the remaining keys:
        _used_id, volume_secret = sa_auth.unwrap_volume_secret(
            profile, provider, filtered)
        volume_secret.zeroize()

        # Physical proof of another valid key succeeded -> perform removal
        broker.credentials.remove(profile_id, target.credential_id)
        broker.audit.write(
            "enroll_removed", profile_id=profile_id, result="ok",
            credential_id_hash=sa_crypto.credential_id_hash(target.credential_id),
            reason="credential removed with authentication")
        return EnrollmentResult(
            True, profile_id,
            credential_id_hash=sa_crypto.credential_id_hash(target.credential_id),
            reason="credential removed after verifying remaining key")


def enrolled_keys(broker, profile_id):
    """Non-secret summary of the enrolled credentials, for the GUI."""
    out = []
    for enrollment in broker.credentials.enrollments(profile_id):
        out.append({
            "credential_profile": enrollment.credential_profile,
            "provider": enrollment.provider,
            "credential_id_hash": sa_crypto.credential_id_hash(enrollment.credential_id),
            "created": enrollment.created,
        })
    return out


_AUDIT_CATEGORIES = {
    "auth_capability", "auth_cancelled", "auth_wrong_credential",
    "auth_unavailable", "auth_failed", "storage_create_failed",
    "config_invalid", "not_enrolled", "unwrap_failed", "internal",
}


# --------------------------------------------------------------------------
# application import
# --------------------------------------------------------------------------
IMPORT_STAGING_PREFIX = "app.import-"
IMPORT_BACKUP_PREFIX = "app.previous-"


def _settle_interrupted_import(destination):
    """Undo what an interrupted import left next to *destination*.

    A staging copy is never production and is dropped. A backup is the
    known-good tree: it is restored when production is missing, and when
    both exist nothing is chosen silently -- the import refuses instead.
    """
    import shutil

    parent = os.path.dirname(destination)
    if not os.path.isdir(parent):
        return
    names = os.listdir(parent)
    for name in names:
        if name.startswith(IMPORT_STAGING_PREFIX):
            shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
    backups = [os.path.join(parent, n) for n in names
               if n.startswith(IMPORT_BACKUP_PREFIX)]
    if not backups:
        return
    present = os.path.isdir(destination) and bool(os.listdir(destination))
    if not present and len(backups) == 1:
        if os.path.isdir(destination):
            os.rmdir(destination)
        os.rename(backups[0], destination)
        return
    raise EnrollmentRefused(
        "an interrupted application import left %s beside %s; resolve it "
        "manually before importing again" % (", ".join(backups), destination),
        "config_invalid")


def import_application(broker, profile_id, source, overwrite=False):
    """Copy a portable application directory into managed storage.

    Optional convenience, not a security control: the executable is not
    encrypted and does not need to be. What it buys is ownership -- after the
    import, the profile launches a copy SAITULS placed, so an update or a
    replacement somewhere else on disk cannot silently become what the
    protected launch runs.

    Refuses to write anywhere but ``<managed-root>\apps\<profile>\app`` and
    refuses to finish unless the profile's declared executable exists in the
    result.
    """
    import shutil

    import sa_paths

    profile = broker.registry.get(profile_id)
    if profile.allow_unmanaged_executable:
        raise EnrollmentRefused(
            "profile %r declares allow_unmanaged_executable, so it launches a "
            "copy outside managed storage; importing would have no effect"
            % profile_id, "config_invalid")
    source_dir = sa_paths.canonical(source)
    if not os.path.isdir(source_dir):
        raise EnrollmentRefused("application source not found: %s" % source_dir,
                                "path_policy")
    destination = sa_paths.assert_managed(
        profile.managed_root,
        os.path.join(profile.managed_root, "apps", profile.id, "app"),
        "application import destination")
    if (sa_paths.is_within(source_dir, destination)
            or sa_paths.is_within(destination, source_dir)):
        raise EnrollmentRefused(
            "refusing to import between %s and %s: one contains the other"
            % (source_dir, destination), "path_policy")
    _settle_interrupted_import(destination)
    # Everything that can refuse is decided before production is touched
    # (SRC-027 W2-005): the declared executable must already be in the source.
    executable = sa_paths.canonical(profile.executable)
    if not sa_paths.is_within(destination, executable):
        raise EnrollmentRefused(
            "the profile's executable %s is not inside the managed application "
            "directory %s" % (executable, destination), "config_invalid")
    relative = os.path.relpath(executable, destination)
    if not os.path.isfile(os.path.join(source_dir, relative)):
        raise EnrollmentRefused(
            "the source does not contain the profile's declared executable "
            "(%s); nothing was changed" % relative, "config_invalid")
    parent = os.path.dirname(destination)
    if os.path.isdir(destination) and os.listdir(destination):
        if not overwrite:
            raise EnrollmentRefused(
                "managed application directory already exists: %s (pass "
                "overwrite to replace it)" % destination, "config_invalid")

    # Staged commit on the same volume: copy + validate a sibling, then swap.
    token = uuid.uuid4().hex[:12]
    staging = os.path.join(parent, IMPORT_STAGING_PREFIX + token)
    try:
        shutil.copytree(source_dir, staging)
        if not os.path.isfile(os.path.join(staging, relative)):
            raise EnrollmentRefused(
                "the staged copy lacks the declared executable (%s); nothing "
                "was changed" % relative, "config_invalid")
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    backup = None
    if os.path.lexists(destination):
        backup = os.path.join(parent, IMPORT_BACKUP_PREFIX + token)
        try:
            os.rename(destination, backup)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    try:
        os.rename(staging, destination)
        if not os.path.isfile(profile.executable):
            raise EnrollmentRefused(
                "the import completed but the profile's declared executable is "
                "still missing: %s" % profile.executable, "config_invalid")
    except BaseException:
        # Put the known-good tree back exactly as it was.
        if os.path.isdir(destination) and not os.path.isdir(staging):
            shutil.rmtree(destination, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and not os.path.lexists(destination):
            os.rename(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    broker.audit.write("import_application", profile_id=profile_id, result="ok")
    return {
        "ok": True,
        "profile_id": profile_id,
        "source": source_dir,
        "destination": destination,
        "executable": profile.executable,
        "files": sum(len(files) for _r, _d, files in os.walk(destination)),
    }
