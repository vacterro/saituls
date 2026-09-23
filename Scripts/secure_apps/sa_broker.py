"""The Secure Apps session broker.

One object owns everything that must not be spread across a GUI, a launcher
and a shell script: authentication, the lifetime of the decrypted volume
secret, mounting and unmounting, the protected application's process tree,
the idle policy and the Windows session events.

The single most important distinction in this module -- and the one the
"Default" policy exists to express -- is that these are two different clocks:

    authentication session lifetime   how long the broker may reuse the
                                      decrypted volume secret without asking
                                      for the key again

    vault mounted lifetime            how long the plaintext filesystem is
                                      actually reachable

They are NOT the same. In Default mode the authentication session lasts six
idle hours, but the vault is detached the moment the protected application
exits. Reopening inside the session remounts without a key interaction; the
data was not sitting readable in between. A design that conflated the two
would leave a vault mounted for six hours because somebody authenticated
once, which is the failure this policy is written against.

The second distinction: *protected* activity. Six hours of inactivity means
six hours without protected-application activity -- a protected launch, the
protected application owning the foreground window, or an explicit Secure
Apps interaction. Using an unrelated program keeps nothing alive.

Only this object ever holds the decrypted volume unlock secret, only in
memory, in a :class:`sa_crypto.SecretBuffer` that is zeroized on every exit
path. The GUI, the CLI and the launcher receive states and counters, never
key material.
"""
import os
import threading
import time

import sa_applife
import sa_audit
import sa_auth
import sa_config
import sa_crypto
import sa_privhelper
import sa_state
import sa_storage

from sa_state import (LOCKED, AUTH_REQUIRED, AUTHENTICATING, UNLOCKING, MOUNTED,
                      RUNNING, SESSION_CACHED, LOCKING, RECOVERY_REQUIRED, ERROR)

# System conditions the policy can react to.
EVENT_WORKSTATION_LOCK = "workstation_lock"
EVENT_LOGOFF = "logoff"
EVENT_SUSPEND = "suspend"
EVENT_SHUTDOWN = "shutdown"
EVENT_BROKER_SHUTDOWN = "broker_shutdown"
EVENT_MANUAL_LOCK = "manual_lock"

SYSTEM_EVENTS = (EVENT_WORKSTATION_LOCK, EVENT_LOGOFF, EVENT_SUSPEND,
                 EVENT_SHUTDOWN, EVENT_BROKER_SHUTDOWN, EVENT_MANUAL_LOCK)

_EVENT_POLICY_FLAG = {
    EVENT_WORKSTATION_LOCK: "lock_on_windows_lock",
    EVENT_LOGOFF: "lock_on_logoff",
    EVENT_SUSPEND: "lock_on_suspend",
    EVENT_SHUTDOWN: "lock_on_shutdown",
    EVENT_BROKER_SHUTDOWN: "lock_on_broker_shutdown",
    EVENT_MANUAL_LOCK: None,          # Lock Now is unconditional
}

_EVENT_REAUTH_CONDITION = {
    EVENT_WORKSTATION_LOCK: "require_auth_after_windows_lock",
    EVENT_LOGOFF: "require_auth_after_logoff",
    EVENT_SUSPEND: "require_auth_after_suspend",
}


class BrokerError(Exception):
    category = "internal"

    def __init__(self, message, category=None):
        Exception.__init__(self, message)
        if category:
            self.category = category


class OperationResult(object):
    __slots__ = ("ok", "state", "reason", "error_category", "pid",
                 "required_auth", "profile_id", "detail")

    def __init__(self, ok, state, profile_id, reason="", error_category="none",
                 pid=0, required_auth=False, detail=None):
        self.ok = ok
        self.state = state
        self.profile_id = profile_id
        self.reason = reason
        self.error_category = error_category
        self.pid = pid
        self.required_auth = required_auth
        self.detail = detail or {}

    def to_dict(self):
        return {
            "ok": self.ok,
            "state": self.state,
            "profile_id": self.profile_id,
            "reason": self.reason,
            "error_category": self.error_category,
            "pid": self.pid,
            "required_auth": self.required_auth,
            "detail": self.detail,
        }

    def __repr__(self):
        return "<OperationResult %s %s ok=%s %s>" % (
            self.profile_id, self.state, self.ok, self.reason)


class ProtectedSession(object):
    """The decrypted volume secret plus the two timers that bound it."""

    __slots__ = ("credential_id", "_secret", "created_at", "last_activity",
                 "auth_count")

    def __init__(self, credential_id, secret, now, auth_count=1):
        self.credential_id = bytes(credential_id)
        self._secret = secret
        self.created_at = now
        self.last_activity = now
        self.auth_count = auth_count

    @property
    def secret(self):
        if self._secret is None or self._secret.closed:
            raise BrokerError("the protected session has already been invalidated",
                              "session_expired")
        return self._secret

    @property
    def alive(self):
        return self._secret is not None and not self._secret.closed

    def refresh(self, now):
        self.last_activity = now

    def idle_seconds(self, now):
        return max(0.0, now - self.last_activity)

    def age_seconds(self, now):
        return max(0.0, now - self.created_at)

    def invalidate(self):
        if self._secret is not None:
            self._secret.zeroize()
        self._secret = None


class ProfileRuntime(object):
    """Everything the broker tracks for one profile. Guarded by ``lock``."""

    def __init__(self, profile, supervisor):
        self.profile = profile
        self.supervisor = supervisor
        self.lock = threading.RLock()
        self.state = LOCKED
        self.session = None
        self.pending_reauth = False
        self.pending_reason = ""
        self.locking = False
        self.busy = False
        self.last_error = ""
        self.last_error_category = "none"
        self.recovery_reason = ""
        self.storage_started = False
        self.auth_interactions = 0
        self.last_activity_wall = None
        self.mode_override = None

    @property
    def mode(self):
        return self.mode_override or self.profile.policy.mode


class SingleInstanceGuard(object):
    """One broker per user session. Everything else is a race waiting to run.

    Two brokers would each believe they own the mount, each run an idle timer
    and each be entitled to unmount the volume under the other's running
    application.
    """

    def __init__(self, name="Local\\SAITULS_SECURE_APPS_BROKER"):
        self.name = name
        self._handle = None
        self.acquired = False

    def acquire(self):
        try:
            import win32event
            import win32api
            import winerror
        except ImportError:
            # Without pywin32 the guard degrades to advisory. Say so rather
            # than pretending exclusivity that is not enforced.
            self.acquired = True
            self._handle = None
            return True
        handle = win32event.CreateMutex(None, True, self.name)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass
            self.acquired = False
            return False
        self._handle = handle
        self.acquired = True
        return True

    def release(self):
        if self._handle is not None:
            try:
                import win32api
                win32api.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        self.acquired = False


class SecureBroker(object):
    """Single-instance owner of authentication, storage and app lifetime."""

    def __init__(self, registry, audit=None, state_store=None,
                 credential_store=None, providers=None, backends=None,
                 process_adapter=None, clock=time.monotonic, sleep=time.sleep,
                 auto_start_storage=True, pin_callback=None, window_handle=None):
        self.registry = registry
        # Handed to a provider that can use them. pin_callback is consulted
        # only on the CTAP paths (in-process or through the elevated helper)
        # -- on Windows WebAuthn the platform collects the PIN in its own
        # native UX and it never enters this process. Neither value is ever
        # stored by a provider. See set_pin_callback for the signature.
        self._pin_callback = pin_callback
        self.window_handle = window_handle
        self.audit = audit or sa_audit.NullAuditLog()
        self.state_store = state_store or sa_state.StateStore(registry.state_path)
        self.credentials = credential_store or sa_auth.CredentialStore(
            registry.credentials_path)
        self._test_providers = providers or {}
        self._test_backends = backends or {}
        self.clock = clock
        self.sleep = sleep
        self.auto_start_storage = auto_start_storage
        # Durable exposed-state records are bound to this instance, not to a
        # reusable pid (SRC-027 W2-003).
        (self.broker_pid, self.broker_nonce,
         self.broker_birth_time) = sa_state.broker_instance_identity()
        self._backends = {}
        self._providers = {}
        self._runtimes = {}
        self._helper = None
        self._global_lock = threading.RLock()
        self._shutting_down = False
        for profile in registry.profiles:
            supervisor = sa_applife.AppSupervisor(
                adapter=process_adapter or sa_applife.ProcessAdapter(),
                clock=clock, sleep=sleep)
            self._runtimes[profile.id] = ProfileRuntime(profile, supervisor)

    def _get_helper(self):
        with self._global_lock:
            if self._helper is None:
                import sa_privhelper
                self._helper = sa_privhelper.PrivilegedHelper()
            return self._helper

    @property
    def helper_process_id(self):
        """Kernel-verified pid of the running elevated helper, else None.

        Diagnostics only. The disposable hardware acceptance uses it to kill
        the helper on purpose and prove a dead helper never reads as LOCKED.
        """
        with self._global_lock:
            helper = self._helper
            if helper is None or not getattr(helper, "running", False):
                return None
            return getattr(getattr(helper, "identity", None), "pid", None)

    # -- the pre-authorized privilege boundary ----------------------------
    def ensure_privileged_helper(self):
        """A verified high-integrity helper, started silently if it is gone.

        Reuse first: a helper this broker has already proven is simply used
        again. Otherwise Task Scheduler is asked to start the one registered,
        fingerprinted task, the pipe is awaited for a bounded time, and the
        process that answers is verified from the kernel before anything is
        sent to it. No consent dialog is raised on this path -- that is the
        whole point of the scheduled-task boundary -- and a helper that
        cannot be started is reported, never worked around.
        """
        if os.name != "nt":
            raise BrokerError("the privileged helper is a Windows component",
                              "privileged_helper_unavailable")
        helper = self._get_helper()
        if getattr(helper, "running", False):
            return helper
        helper.ensure()
        return helper

    def privileged_helper_status(self):
        """What the UAC diagnostics report shows. Measures; decides nothing."""
        import sa_privtask
        report = sa_privtask.preflight()
        with self._global_lock:
            helper = self._helper
        identity = getattr(helper, "identity", None) if helper is not None else None
        running = bool(helper is not None and getattr(helper, "running", False))
        report["broker_pid"] = self.broker_pid
        report["helper_connected"] = running
        report["helper_pid"] = getattr(identity, "pid", None) if running else None
        report["helper_integrity"] = (
            ("HIGH" if getattr(identity, "elevated", None) else "MEDIUM")
            if running else None)
        report["fido_worker_integrity"] = "HIGH" if running else None
        report["pipe_authentication"] = "VERIFIED" if running else "NOT_CONNECTED"
        report["helper_leases"] = getattr(helper, "leases", 0) if helper else 0
        return report

    def _requires_privileged_helper(self, profile):
        """True when detaching this profile's storage really needs the helper.

        The one case that is answered locally is a container that does not
        exist: there is nothing attached, nothing to detach, and no reason to
        start an elevated process to be told so. Every other answer -- including
        "it is probably already detached" -- needs the helper, because the
        alternative is reporting LOCKED on a guess.
        """
        if os.name != "nt":
            return False
        try:
            backend = self.backend(profile)
        except Exception:
            return False
        if not getattr(backend, "requires_elevation", False):
            return False
        try:
            if not os.path.isfile(profile.container):
                return False
        except (OSError, TypeError):
            pass
        return True

    def _ensure_helper_for_lock(self, runtime, reason):
        """``(ready, failure)``. A lock may never be reported without one.

        Relocking detaches a volume, and detaching is privileged. If the
        helper cannot be started, the honest answer is
        ``LOCK_PRIVILEGED_HELPER_UNAVAILABLE`` and an ERROR state: the vault
        may still be mounted, and a comfortable-looking LOCKED would be a lie
        about whether the user's data is readable right now.
        """
        profile = runtime.profile
        if not self._requires_privileged_helper(profile):
            return True, None
        try:
            self.ensure_privileged_helper()
            return True, None
        except Exception as exc:
            import sa_privtask
            token = getattr(exc, "token", "") or \
                sa_privtask.LOCK_PRIVILEGED_HELPER_UNAVAILABLE
            category = getattr(exc, "category", "privileged_helper_unavailable")
            if category not in sa_audit.ERROR_CATEGORIES:
                category = "privileged_helper_unavailable"
            runtime.last_error = "%s: %s" % (token, exc)
            runtime.last_error_category = category
            self._set_state(runtime, ERROR, "lock_failed")
            self._log(runtime, "lock_failed", result="failed",
                      error_category=category, event=reason)
            return False, OperationResult(False, ERROR, profile.id,
                                          runtime.last_error, category)

    # -- PIN callback -----------------------------------------------------
    @property
    def pin_callback(self):
        return self._pin_callback

    @pin_callback.setter
    def pin_callback(self, callback):
        self.set_pin_callback(callback)

    def set_pin_callback(self, callback):
        """Replace the PIN callback, including in providers already built.

        ``callback(rp_id) -> str | None``; None means the PIN prompt was
        cancelled. A provider is built once per profile and keeps its own
        reference, so rebinding only this broker's attribute would leave the
        next assertion calling the old callback. Assigning ``pin_callback``
        routes here too.
        """
        with self._global_lock:
            self._pin_callback = callback
            for provider in self._providers.values():
                if hasattr(provider, "pin_callback"):
                    provider.pin_callback = callback
        return callback

    # -- component wiring -------------------------------------------------
    def backend(self, profile):
        with self._global_lock:
            if profile.id not in self._backends:
                backend = sa_storage.build_backend(
                    profile, self._test_backends)
                if hasattr(backend, "_helper") and backend._helper is None and os.name == "nt":
                    backend._helper = self._get_helper()
                self._backends[profile.id] = backend
            return self._backends[profile.id]

    def provider(self, profile):
        with self._global_lock:
            if profile.id not in self._providers:
                helper = self._get_helper() if os.name == "nt" else None
                provider = sa_auth.build_provider(profile, self._test_providers, helper=helper)
                if self.pin_callback is not None and hasattr(provider, "pin_callback"):
                    provider.pin_callback = self.pin_callback
                if self.window_handle is not None and hasattr(provider, "window_handle"):
                    provider.window_handle = self.window_handle
                if helper is not None and hasattr(provider, "helper") and getattr(provider, "helper", None) is None:
                    provider.helper = helper
                required = profile.policy.condition("require_auth_provider")
                if required and provider.name != required.params["provider"]:
                    raise BrokerError(
                        "profile %r requires authentication provider %r but %r "
                        "was built" % (profile.id, required.params["provider"],
                                       provider.name),
                        "config_invalid")
                if not getattr(provider, "provides_key_material", True):
                    raise BrokerError(
                        "provider %r cannot produce key material and must not be "
                        "used for an encrypted-storage profile" % provider.name,
                        "auth_capability")
                self._providers[profile.id] = provider
            return self._providers[profile.id]

    def runtime(self, profile_id):
        try:
            return self._runtimes[profile_id]
        except KeyError:
            raise BrokerError("unknown profile id: %r" % (profile_id,), "config_invalid")

    # -- policy helpers ---------------------------------------------------
    @staticmethod
    def _unmount_on_exit(profile):
        policy = profile.policy
        return bool(policy.unmount_when_app_closes
                    or policy.has("unmount_storage_on_app_exit"))

    def _idle_limit_seconds(self, profile):
        return profile.policy.idle_timeout_minutes * 60.0

    def _hard_lifetime_seconds(self, profile):
        cond = profile.policy.condition("require_auth_after_minutes")
        return cond.params["minutes"] * 60.0 if cond else None

    def _session_valid(self, runtime, now):
        session = runtime.session
        if session is None or not session.alive:
            return False
        if runtime.pending_reauth:
            return False
        profile = runtime.profile
        if session.idle_seconds(now) >= self._idle_limit_seconds(profile):
            return False
        hard = self._hard_lifetime_seconds(profile)
        if hard is not None and session.age_seconds(now) >= hard:
            return False
        return True

    # -- audit ------------------------------------------------------------
    def _log(self, runtime, transition, result="ok", **fields):
        profile = runtime.profile
        fields.setdefault("state", runtime.state)
        fields.setdefault("mount_path", profile.mount_path)
        fields.setdefault("auth_provider", profile.provider)
        fields.setdefault("backend", profile.backend)
        fields.setdefault("container_id", profile.container_id)
        fields.setdefault("policy_mode", runtime.mode)
        if runtime.session is not None and runtime.session.alive:
            fields.setdefault("credential_id_hash",
                              sa_crypto.credential_id_hash(runtime.session.credential_id))
        return self.audit.write(transition, profile_id=profile.id, result=result,
                                **fields)

    def _set_state(self, runtime, state, transition, pid=0, persist=True):
        runtime.state = state
        if persist:
            try:
                self.state_store.record(
                    runtime.profile.id, runtime.profile.container,
                    runtime.profile.mount_path, state, transition,
                    last_pid=pid or runtime.supervisor.pid,
                    broker_pid=self.broker_pid,
                    broker_nonce=self.broker_nonce,
                    broker_birth_time=self.broker_birth_time)
            except Exception:
                # A state file that cannot be written must not stop a lock.
                self._log(runtime, "state_write_failed", result="failed",
                          error_category="internal")
        return state

    # -- reconciliation ---------------------------------------------------
    def reconcile(self, profile_id):
        """Compare recorded state against reality. Pessimistic; see sa_state."""
        runtime = self.runtime(profile_id)
        profile = runtime.profile
        with runtime.lock:
            try:
                backend = self.backend(profile)
                if self.auto_start_storage and not runtime.storage_started:
                    backend.start()
                    runtime.storage_started = True
                storage_state = backend.state(profile)
            except (sa_storage.StorageError, sa_privhelper.HelperError) as exc:
                runtime.last_error = str(exc)
                runtime.last_error_category = getattr(exc, "category",
                                                      "storage_mount_failed")
                self._set_state(runtime, ERROR, "reconcile")
                self._log(runtime, "reconcile", result="failed",
                          error_category=runtime.last_error_category)
                return sa_state.Reconciliation(profile.id, runtime.state, "unknown",
                                               ERROR, str(exc), False)
            if storage_state == sa_storage.MISSING:
                self._set_state(runtime, LOCKED, "reconcile")
                self._log(runtime, "reconcile", result="ok", detail_code="no_container")
                return sa_state.Reconciliation(profile.id, LOCKED, storage_state,
                                               LOCKED, "no container yet", True)
            verdict = sa_state.reconcile(profile, self.state_store, storage_state,
                                         broker_nonce=self.broker_nonce,
                                         broker_birth_time=self.broker_birth_time)
            if verdict.verdict == RECOVERY_REQUIRED:
                runtime.recovery_reason = verdict.reason
                self._set_state(runtime, RECOVERY_REQUIRED, "reconcile")
                self._log(runtime, "reconcile", result="refused",
                          error_category="recovery_required")
            elif verdict.verdict == LOCKED and not runtime.supervisor.running:
                if not self._session_valid(runtime, self.clock()):
                    self._set_state(runtime, LOCKED, "reconcile")
                else:
                    self._set_state(runtime, SESSION_CACHED, "reconcile")
                self._log(runtime, "reconcile", result="ok")
            elif verdict.verdict in sa_state.STORAGE_EXPOSED_STATES:
                # This exact instance owns the exposed volume: the runtime
                # must say so too, never a stale LOCKED beside a live mount.
                if runtime.state != verdict.verdict:
                    self._set_state(runtime, verdict.verdict, "reconcile",
                                    persist=False)
                self._log(runtime, "reconcile", result="ok",
                          detail_code="owned_by_this_instance")
            return verdict

    def reconcile_all(self):
        return {pid: self.reconcile(pid) for pid in self._runtimes}

    def recover(self, profile_id):
        """Attempt the safe relock a RECOVERY_REQUIRED profile needs."""
        runtime = self.runtime(profile_id)
        with runtime.lock:
            if runtime.state != RECOVERY_REQUIRED:
                return OperationResult(True, runtime.state, profile_id,
                                       "nothing to recover")
            self._log(runtime, "recovery_begin")
            result = self._secure_lock(runtime, "recovery")
            if result.ok:
                runtime.recovery_reason = ""
            return result

    # -- open -------------------------------------------------------------
    def authenticate_only(self, profile_id, force_auth=False):
        """Establish a session WITHOUT mounting anything.

        Migration needs the volume secret before the profile's real mount
        path is free -- on a first run that path still holds the plaintext
        vault. Going through :meth:`open` would mean mounting there, which is
        precisely the circular dependency the staging design removes. So this
        entry point authenticates and stops: the result state is
        ``SESSION_CACHED`` (session valid, storage detached), and the caller
        mounts wherever it needs to.
        """
        runtime = self.runtime(profile_id)
        profile = runtime.profile
        if not profile.enabled:
            return OperationResult(False, runtime.state, profile_id,
                                   "profile is disabled", "config_invalid")
        if not runtime.lock.acquire(timeout=0.0):
            self._log(runtime, "authenticate_only", result="refused",
                      error_category="concurrent_request")
            return OperationResult(False, runtime.state, profile_id,
                                   "another Secure Apps operation is in progress "
                                   "for this profile", "concurrent_request")
        try:
            now = self.clock()
            if self._shutting_down or runtime.locking:
                self._log(runtime, "authenticate_only", result="refused",
                          error_category="concurrent_request")
                return OperationResult(False, runtime.state, profile_id,
                                       "the profile is locking; new sessions are "
                                       "not accepted", "concurrent_request")
            if runtime.state == RECOVERY_REQUIRED:
                self._log(runtime, "authenticate_only", result="refused",
                          error_category="recovery_required")
                return OperationResult(False, RECOVERY_REQUIRED, profile_id,
                                       runtime.recovery_reason or
                                       "previous session did not relock cleanly",
                                       "recovery_required")
            if not force_auth and self._session_valid(runtime, now):
                runtime.session.refresh(now)
                return OperationResult(True, runtime.state, profile_id,
                                       "the authentication session is already "
                                       "valid")
            try:
                self._authenticate(runtime, now)
            except Exception as exc:
                return self._fail(runtime, "authenticate", exc, "auth_failed")
            if runtime.state != MOUNTED and runtime.state != RUNNING:
                self._set_state(runtime, SESSION_CACHED, "authenticate_only")
            return OperationResult(True, runtime.state, profile_id,
                                   "authenticated; the volume stays detached")
        finally:
            runtime.lock.release()

    def open(self, profile_id, force_auth=False):
        """Authenticate if required, mount, launch. The one protected entry."""
        runtime = self.runtime(profile_id)
        profile = runtime.profile
        if not profile.enabled:
            return OperationResult(False, runtime.state, profile_id,
                                   "profile is disabled", "config_invalid")
        acquired = runtime.lock.acquire(timeout=0.0)
        if not acquired:
            # A second launch arriving while the first is still authenticating
            # or mounting is a race, not a queue: refusing it is what stops two
            # FIDO2 prompts and two mount attempts for one user intent.
            self._log(runtime, "open", result="refused",
                      error_category="concurrent_request")
            return OperationResult(False, runtime.state, profile_id,
                                   "another Secure Apps operation is in progress "
                                   "for this profile", "concurrent_request")
        try:
            return self._open_locked(runtime, force_auth)
        finally:
            runtime.lock.release()

    def _open_locked(self, runtime, force_auth):
        profile = runtime.profile
        now = self.clock()
        if self._shutting_down or runtime.locking:
            self._log(runtime, "open", result="refused",
                      error_category="concurrent_request")
            return OperationResult(False, runtime.state, profile.id,
                                   "the profile is locking; new launches are "
                                   "not accepted", "concurrent_request")
        if runtime.state == RECOVERY_REQUIRED:
            self._log(runtime, "open", result="refused",
                      error_category="recovery_required")
            return OperationResult(False, RECOVERY_REQUIRED, profile.id,
                                   runtime.recovery_reason or
                                   "previous session did not relock cleanly",
                                   "recovery_required")

        # Already running: the launch transaction is satisfied by the live
        # instance. Aggressive mode's "unless the exact protected application
        # is already running as part of the current launch transaction" is
        # exactly this branch, and it never re-prompts.
        if runtime.supervisor.running:
            self.note_activity(profile.id)
            self._log(runtime, "open", result="noop", error_category="already_running",
                      pid=runtime.supervisor.pid)
            return OperationResult(True, RUNNING, profile.id,
                                   "the protected application is already running",
                                   "already_running", pid=runtime.supervisor.pid)

        backend = self.backend(profile)
        try:
            if self.auto_start_storage and not runtime.storage_started:
                backend.start()
                runtime.storage_started = True
        except Exception as exc:
            return self._fail(runtime, "open", exc, "storage_mount_failed")

        if backend.state(profile) == sa_storage.MISSING:
            self._log(runtime, "open", result="refused", error_category="not_enrolled")
            return OperationResult(False, LOCKED, profile.id,
                                   "no protected container exists yet; run the "
                                   "migration or create the vault first",
                                   "not_enrolled")

        needs_auth = force_auth or runtime.mode == "aggressive" or not self._session_valid(runtime, now)
        try:
            if needs_auth:
                self._authenticate(runtime, now)
            else:
                runtime.session.refresh(now)
                self._log(runtime, "session_reused", result="ok",
                          detail_code="no_auth_required")
        except (sa_auth.AuthError, sa_crypto.CryptoError, BrokerError) as exc:
            return self._fail(runtime, "authenticate", exc, "auth_failed")

        # -- unlock + mount -------------------------------------------------
        self._set_state(runtime, UNLOCKING, "unlock")
        try:
            backend.unlock_and_mount(profile, runtime.session.secret)
        except Exception as exc:
            return self._fail(runtime, "unlock", exc, "storage_mount_failed")
        if not os.path.isdir(profile.mount_path):
            exc = sa_storage.StorageError(
                "the protected volume reported mounted but %s does not exist"
                % profile.mount_path, "storage_mount_failed")
            return self._fail(runtime, "verify_mount", exc, "storage_mount_failed")
        self._set_state(runtime, MOUNTED, "mount")
        self._log(runtime, "mount", result="ok")

        # -- launch ----------------------------------------------------------
        try:
            arguments = self._build_arguments(profile)
            pid = runtime.supervisor.launch(profile.executable, arguments,
                                            profile.working_directory)
        except Exception as exc:
            # A mount without an application is an exposed vault: relock it
            # rather than leaving the failure sitting there readable.
            self._log(runtime, "launch", result="failed",
                      error_category="app_launch_failed")
            self._secure_lock(runtime, "launch_failed")
            runtime.last_error = str(exc)
            runtime.last_error_category = "app_launch_failed"
            return OperationResult(False, runtime.state, profile.id, str(exc),
                                   "app_launch_failed")
        self._set_state(runtime, RUNNING, "launch", pid=pid)
        runtime.session.refresh(self.clock())
        runtime.last_activity_wall = time.time()
        self._log(runtime, "launch", result="ok", pid=pid)
        return OperationResult(True, RUNNING, profile.id, "protected application started",
                               pid=pid, required_auth=needs_auth)

    def _build_arguments(self, profile):
        """Executable and arguments stay separate; nothing is parsed from text.

        ``vault_argument_style`` decides how the mounted vault is named to the
        application. The URI form is only ever produced AFTER the filesystem
        is mounted, and it is passed to the profile's own executable -- never
        handed to the shell, so a globally registered ``obsidian://`` handler
        cannot redirect the launch to a different Obsidian.
        """
        arguments = list(profile.arguments)
        if profile.vault_argument_style == "path":
            arguments.append(profile.mount_path)
        elif profile.vault_argument_style == "obsidian-uri":
            from urllib.parse import quote
            arguments.append("obsidian://open?path=" + quote(profile.mount_path, safe=""))
        return arguments

    def _authenticate(self, runtime, now):
        profile = runtime.profile
        provider = self.provider(profile)
        self._set_state(runtime, AUTHENTICATING, "authenticate")
        runtime.auth_interactions += 1
        self._log(runtime, "authenticate_begin")
        if runtime.session is not None:
            runtime.session.invalidate()
            runtime.session = None
        credential_id, secret = sa_auth.unwrap_volume_secret(
            profile, provider, self.credentials)
        runtime.session = ProtectedSession(credential_id, secret, now)
        runtime.pending_reauth = False
        runtime.pending_reason = ""
        self._log(runtime, "authenticate", result="ok",
                  credential_id_hash=sa_crypto.credential_id_hash(credential_id))
        return runtime.session

    def _fail(self, runtime, transition, exc, fallback_category):
        category = getattr(exc, "category", None) or fallback_category
        if category not in sa_audit.ERROR_CATEGORIES:
            category = fallback_category
        runtime.last_error = str(exc)
        runtime.last_error_category = category
        # A failed unlock or mount must not leave a half-open session behind.
        if transition in ("unlock", "verify_mount"):
            self._safe_unmount(runtime)
            self._invalidate_session(runtime, transition)
        # A cancelled or refused key interaction is not a broken subsystem: it
        # is the normal "no key, no vault" outcome and stays offerable.
        if category in ("auth_cancelled", "auth_wrong_credential",
                        "auth_capability", "auth_unavailable", "not_enrolled"):
            state = AUTH_REQUIRED
            result = "cancelled" if category == "auth_cancelled" else "refused"
        else:
            state = ERROR
            result = "failed"
        self._set_state(runtime, state, transition)
        self._log(runtime, transition, result=result, error_category=category)
        return OperationResult(False, state, runtime.profile.id, str(exc), category)

    # -- activity ---------------------------------------------------------
    def note_activity(self, profile_id, source="interaction"):
        """Refresh the protected session clock. Only protected sources qualify."""
        runtime = self.runtime(profile_id)
        with runtime.lock:
            if runtime.session is not None and runtime.session.alive:
                runtime.session.refresh(self.clock())
                runtime.last_activity_wall = time.time()
                return True
            return False

    def poll_foreground(self, snapshots=None):
        """Refresh sessions whose protected application owns the foreground.

        When *snapshots* maps profile_id -> ProcessObservation, one
        already‑captured tree/foreground per profile drives both the RUNNING
        test and the foreground membership test (SRC-027 PERF-004).
        """
        refreshed = []
        for profile_id, runtime in self._runtimes.items():
            with runtime.lock:
                obs = snapshots.get(profile_id) if snapshots else None
                running = runtime.supervisor.running_from(obs)
                fg = runtime.supervisor.foreground_is_protected_from(obs)
                if not running:
                    continue
                if fg:
                    if runtime.session is not None and runtime.session.alive:
                        runtime.session.refresh(self.clock())
                        runtime.last_activity_wall = time.time()
                        refreshed.append(profile_id)
        return refreshed

    # -- periodic ---------------------------------------------------------
    def tick(self, snapshots=None):
        """Advance every profile: app exits first, then the idle policy.

        *snapshots* is the caller's per‑turn observation cache (PERF‑004);
        when present, running/foreground derive from it instead of re‑walking.
        """
        events = []
        for profile_id in list(self._runtimes):
            runtime = self._runtimes[profile_id]
            if not runtime.lock.acquire(timeout=0.0):
                continue
            try:
                events.extend(self._tick_profile(runtime, snapshots=snapshots))
            finally:
                runtime.lock.release()
        return events

    def _tick_profile(self, runtime, snapshots=None):
        events = []
        profile = runtime.profile
        now = self.clock()
        pid = getattr(runtime.profile, 'id', None)
        obs = snapshots.get(pid) if snapshots else None
        running = runtime.supervisor.running_from(obs)
        if runtime.state == RUNNING and not running:
            runtime.supervisor.forget()
            events.append(self._handle_app_exit(runtime))
        if runtime.session is not None and runtime.session.alive:
            idle = runtime.session.idle_seconds(now)
            hard = self._hard_lifetime_seconds(profile)
            expired = idle >= self._idle_limit_seconds(profile)
            if not expired and hard is not None:
                expired = runtime.session.age_seconds(now) >= hard
            if expired:
                events.append(self._handle_idle_expiry(runtime, observation=obs))
        return [e for e in events if e is not None]

    def _handle_app_exit(self, runtime):
        profile = runtime.profile
        self._log(runtime, "app_exit", result="ok")
        if self._unmount_on_exit(profile):
            # Closing Obsidian detaches the vault, which is privileged. Same
            # rule as the lock sequence: start the registered task silently,
            # and say so when it cannot be started instead of leaving a
            # detached-looking state over a mounted volume.
            ready, failure = self._ensure_helper_for_lock(runtime, "app_exit")
            if not ready:
                return failure
            unmounted = self._safe_unmount(runtime)
            if not unmounted:
                self._set_state(runtime, ERROR, "app_exit_unmount")
                return OperationResult(False, ERROR, profile.id,
                                       "the protected volume could not be detached "
                                       "after the application exited",
                                       runtime.last_error_category or
                                       "storage_unmount_failed")
        aggressive = runtime.mode == "aggressive"
        drop_session = aggressive or profile.policy.has("require_auth_after_app_exit")
        if drop_session:
            self._invalidate_session(runtime, "app_exit")
            self._set_state(runtime, LOCKED, "app_exit")
            self._log(runtime, "session_invalidated", result="ok",
                      detail_code="aggressive" if aggressive else "condition")
            return OperationResult(True, LOCKED, profile.id,
                                   "application exited; session invalidated")
        # Default: the authentication session survives, the vault does not.
        self._set_state(runtime, SESSION_CACHED, "app_exit")
        self._log(runtime, "session_cached", result="ok")
        return OperationResult(True, SESSION_CACHED, profile.id,
                               "application exited; vault detached, session cached")

    def _handle_idle_expiry(self, runtime, observation=None):
        profile = runtime.profile
        self._log(runtime, "idle_expiry", result="ok")
        app_running = (runtime.supervisor.running_from(observation)
                       if observation is not None
                       else runtime.supervisor.running)
        must_close = app_running and (
            profile.policy.has("close_app_on_idle")
            or runtime.state in (RUNNING, MOUNTED))
        if must_close:
            result = self._secure_lock(runtime, "idle")
            return result
        self._invalidate_session(runtime, "idle")
        if not app_running:
            ok, err_cat = self._ensure_detached(runtime, "idle")
            if not ok:
                self._set_state(runtime, ERROR, "idle_unmount_failed")
                self._log(runtime, "idle_expiry", result="failed",
                          error_category=err_cat or "storage_unmount_failed")
                return OperationResult(False, ERROR, profile.id,
                                       runtime.last_error or "unmount failed during idle expiry",
                                       err_cat or "storage_unmount_failed")
        self._set_state(runtime, LOCKED, "idle")
        self._log(runtime, "session_expired", result="ok",
                  error_category="session_expired")
        return OperationResult(True, LOCKED, profile.id,
                               "protected session expired after inactivity")

    # -- system events ----------------------------------------------------
    def on_system_event(self, event, profile_id=None):
        """Apply the profile's policy for a Windows session / power event."""
        if event not in SYSTEM_EVENTS:
            raise BrokerError("unknown system event: %r" % (event,), "internal")
        targets = ([self.runtime(profile_id)] if profile_id
                   else list(self._runtimes.values()))
        results = []
        for runtime in targets:
            with runtime.lock:
                results.append(self._apply_system_event(runtime, event))
        return results

    def _apply_system_event(self, runtime, event):
        profile = runtime.profile
        flag = _EVENT_POLICY_FLAG.get(event)
        enabled = True if flag is None else bool(getattr(profile.policy, flag))
        self._log(runtime, "system_event", result="ok" if enabled else "noop",
                  event=event)
        if not enabled:
            return OperationResult(True, runtime.state, profile.id,
                                   "policy does not lock on %s" % event)
        condition = _EVENT_REAUTH_CONDITION.get(event)
        if condition and profile.policy.has(condition):
            runtime.pending_reauth = True
            runtime.pending_reason = event
        result = self._secure_lock(runtime, event)
        if event in (EVENT_SHUTDOWN, EVENT_BROKER_SHUTDOWN, EVENT_LOGOFF):
            runtime.pending_reauth = True
            runtime.pending_reason = event
        # A profile that will demand the key again should say so, instead of
        # showing a plain LOCKED that looks identical to "reopen is free".
        if result.ok and runtime.pending_reauth:
            self._set_state(runtime, AUTH_REQUIRED, "await_reauth")
            result.state = AUTH_REQUIRED
        return result

    def lock_now(self, profile_id=None):
        return self.on_system_event(EVENT_MANUAL_LOCK, profile_id)

    # -- the secure-lock sequence ----------------------------------------
    def _secure_lock(self, runtime, reason):
        """The nine-step relock. Storage is never detached under open handles.

        1 stop accepting launches   2 request graceful close
        3 bounded wait              4 optional forced termination
        5 confirm exit              6 flush filesystem state
        7 unmount                   8 zero the secret
        9 mark LOCKED
        """
        profile = runtime.profile
        policy = profile.policy
        runtime.locking = True
        previous = runtime.state
        self._set_state(runtime, LOCKING, "lock_begin")
        self._log(runtime, "lock_begin", result="ok", event=reason)
        try:
            exited, forced = runtime.supervisor.close_sequence(
                policy.graceful_close_timeout_seconds,
                policy.force_terminate_after_timeout,
                policy.process_exit_confirm_timeout_seconds)
            if forced:
                self._log(runtime, "app_force_terminated", result="ok", event=reason)
            if not exited:
                # Step 5 failed. Detaching now is the one thing that could lose
                # the user's notes, so the sequence stops here, loudly.
                runtime.last_error = ("the protected application did not exit; "
                                      "storage was NOT detached")
                runtime.last_error_category = "app_exit_timeout"
                self._set_state(runtime, ERROR, "lock_failed")
                self._log(runtime, "lock_failed", result="timeout",
                          error_category="app_exit_timeout", event=reason)
                return OperationResult(False, ERROR, profile.id,
                                       runtime.last_error, "app_exit_timeout")
            # Step 6 needs the elevated helper, and locking must never depend
            # on a consent dialog: the registered task is started silently, and
            # a helper that will not come is a surfaced failure rather than a
            # LOCKED the storage cannot back up.
            ready, failure = self._ensure_helper_for_lock(runtime, reason)
            if not ready:
                return failure
            self._flush_storage(runtime)
            unmounted = self._safe_unmount(runtime)
            self._invalidate_session(runtime, reason)
            if not unmounted:
                self._set_state(runtime, ERROR, "lock_failed")
                self._log(runtime, "lock_failed", result="failed",
                          error_category=runtime.last_error_category, event=reason)
                return OperationResult(False, ERROR, profile.id,
                                       runtime.last_error or "unmount failed",
                                       runtime.last_error_category)
            self._set_state(runtime, LOCKED, "lock")
            self._log(runtime, "lock", result="ok", event=reason)
            return OperationResult(True, LOCKED, profile.id,
                                   "profile locked (%s)" % reason,
                                   detail={"previous_state": previous})
        finally:
            runtime.locking = False

    def _flush_storage(self, runtime):
        """Best-effort flush of the mounted volume before detaching.

        Windows flushes on dismount, but an explicit FlushFileBuffers on the
        volume handle turns "probably written" into "written" for the case
        where the dismount itself later fails and the user retries.
        """
        profile = runtime.profile
        if not os.path.isdir(profile.mount_path):
            return False
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            GENERIC_WRITE = 0x40000000
            FILE_SHARE = 0x00000001 | 0x00000002 | 0x00000004
            OPEN_EXISTING = 3
            FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
            kernel32.CreateFileW.restype = wintypes.HANDLE
            handle = kernel32.CreateFileW(profile.mount_path, GENERIC_WRITE,
                                          FILE_SHARE, None, OPEN_EXISTING,
                                          FILE_FLAG_BACKUP_SEMANTICS, None)
            if handle in (None, 0) or handle == wintypes.HANDLE(-1).value:
                return False
            try:
                kernel32.FlushFileBuffers(handle)
            finally:
                kernel32.CloseHandle(handle)
            self._log(runtime, "flush_storage", result="ok")
            return True
        except Exception:
            self._log(runtime, "flush_storage", result="noop")
            return False

    def _ensure_detached(self, runtime, reason):
        """Ensure storage is physically detached. Returns (ok, error_category)."""
        profile = runtime.profile
        try:
            backend = self.backend(profile)
        except Exception as exc:
            runtime.last_error = str(exc)
            runtime.last_error_category = "storage_unmount_failed"
            return False, "storage_unmount_failed"
        try:
            state = backend.state(profile)
        except Exception as exc:
            runtime.last_error = str(exc)
            runtime.last_error_category = "storage_state_failed"
            self._log(runtime, "ensure_detached_state", result="failed",
                      error_category="storage_state_failed", event=reason)
            return False, "storage_state_failed"
        if state in (sa_storage.DETACHED, sa_storage.MISSING):
            return True, None
        if state == sa_storage.ATTACHED_LOCKED:
            return False, "storage_attached_locked"
        try:
            backend.unmount(profile)
            self._log(runtime, "unmount", result="ok", event=reason)
        except Exception as exc:
            runtime.last_error = str(exc)
            runtime.last_error_category = getattr(exc, "category",
                                                  "storage_unmount_failed")
            self._log(runtime, "unmount", result="failed",
                      error_category=runtime.last_error_category, event=reason)
            return False, runtime.last_error_category
        try:
            state = backend.state(profile)
        except Exception as exc:
            runtime.last_error = str(exc)
            runtime.last_error_category = "storage_state_failed"
            self._log(runtime, "ensure_detached_recheck", result="failed",
                      error_category="storage_state_failed", event=reason)
            return False, "storage_state_failed"
        if state not in (sa_storage.DETACHED, sa_storage.MISSING):
            runtime.last_error = "storage still %s after unmount" % state
            runtime.last_error_category = "storage_unmount_failed"
            self._log(runtime, "ensure_detached_post", result="failed",
                      error_category="storage_unmount_failed", event=reason)
            return False, "storage_unmount_failed"
        return True, None

    def _safe_unmount(self, runtime):
        ok, _cat = self._ensure_detached(runtime, "safe_unmount")
        return ok

    def _invalidate_session(self, runtime, reason):
        if runtime.session is not None:
            runtime.session.invalidate()
            runtime.session = None
            self._log(runtime, "secret_zeroized", result="ok", event=reason)
            return True
        return False

    # -- mode -------------------------------------------------------------
    def set_mode(self, profile_id, mode):
        if mode not in sa_config.POLICY_MODES:
            raise BrokerError("unknown policy mode: %r" % (mode,), "config_invalid")
        runtime = self.runtime(profile_id)
        with runtime.lock:
            previous = runtime.mode
            runtime.mode_override = mode
            self._log(runtime, "mode_changed", result="ok", policy_mode=mode)
            # Tightening the policy must take effect now, not at the next
            # launch: switching to aggressive while a session is cached and
            # the app is closed drops the session immediately. It may only be
            # reported LOCKED once storage is proven detached; otherwise it is
            # a surfaced failure, never a comfortable-looking lock over a
            # still-readable volume.
            if mode == "aggressive" and not runtime.supervisor.running:
                if self._invalidate_session(runtime, "mode_aggressive"):
                    ok, err_cat = self._ensure_detached(runtime, "mode_aggressive")
                    if not ok:
                        self._set_state(runtime, ERROR, "mode_changed_unmount_failed")
                        self._log(runtime, "mode_changed", result="failed",
                                  error_category=err_cat or "storage_unmount_failed")
                        return OperationResult(False, ERROR, profile_id,
                                               runtime.last_error or "volume could not be detached during aggressive-mode switch",
                                               err_cat or "storage_unmount_failed")
                    self._set_state(runtime, LOCKED, "mode_changed")
            return OperationResult(True, runtime.state, profile_id,
                                   "mode %s -> %s" % (previous, mode))

    # -- status -----------------------------------------------------------
    def status(self, profile_id, snapshot=None):
        runtime = self.runtime(profile_id)
        profile = runtime.profile
        now = self.clock()
        session = runtime.session
        alive = session is not None and session.alive
        idle_limit = self._idle_limit_seconds(profile)
        app_running = runtime.supervisor.running_from(snapshot)
        app_pid = runtime.supervisor.pid
        return {
            "profile_id": profile.id,
            "label": profile.label,
            "enabled": profile.enabled,
            "state": runtime.state,
            "mode": runtime.mode,
            "session_active": bool(alive),
            "session_idle_seconds": int(session.idle_seconds(now)) if alive else None,
            "session_age_seconds": int(session.age_seconds(now)) if alive else None,
            "session_expires_in_seconds": (int(max(0.0, idle_limit - session.idle_seconds(now)))
                                           if alive else None),
            "idle_timeout_minutes": profile.policy.idle_timeout_minutes,
            "last_activity_epoch": runtime.last_activity_wall,
            "app_running": app_running,
            "app_pid": app_pid,
            "mount_path": profile.mount_path,
            "container": profile.container,
            "backend": profile.backend,
            "provider": profile.provider,
            "enrolled": self.credentials.is_enrolled(profile.id),
            "pending_reauth": runtime.pending_reauth,
            "pending_reason": runtime.pending_reason,
            "recovery_reason": runtime.recovery_reason,
            "last_error": runtime.last_error,
            "last_error_category": runtime.last_error_category,
            "auth_interactions": runtime.auth_interactions,
        }

    def status_all(self, snapshots=None):
        if snapshots:
            return [self.status(pid, snapshot=snapshots.get(pid)) for pid in self._runtimes]
        return [self.status(pid) for pid in self._runtimes]

    # -- shutdown ---------------------------------------------------------
    def shutdown(self):
        """Broker shutdown is a lock event, not a quiet exit."""
        self._shutting_down = True
        results = []
        for runtime in self._runtimes.values():
            with runtime.lock:
                results.append(self._apply_system_event(runtime, EVENT_BROKER_SHUTDOWN))
        # A profile whose relock failed may still be attached: its backend and
        # the shared privileged helper stay alive for recovery (SRC-027 W2-006).
        unresolved = set(r.profile_id for r in results if r is not None and not r.ok)
        for profile_id, backend in list(self._backends.items()):
            if profile_id in unresolved:
                continue
            try:
                backend.stop()
            except Exception:
                pass
        with self._global_lock:
            if self._helper is not None and not unresolved:
                try:
                    self._helper.close()
                except Exception:
                    pass
                self._helper = None
        return results
