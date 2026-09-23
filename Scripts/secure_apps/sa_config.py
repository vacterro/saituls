"""Versioned, fail-closed configuration registry for SAITULS Secure Apps.

The registry is data, never code. Nothing in ``secure_apps.json`` is ever
handed to a shell: an application is described by a separate executable path
and an argument LIST, both validated here, and the launcher spawns the
executable directly. There is no command-string field and no scripting hook,
by design -- see "Universal conditions" in the subsystem README.

Every rule in this module fails closed:

  * an unknown or malformed ``schema_version`` is refused outright;
  * an unknown key anywhere in a profile is refused, because silently
    ignoring it would let a typo disable a lock policy;
  * an unknown backend, provider, policy mode or condition type is refused;
  * a path that escapes its managed root, or crosses a reparse point inside
    it, is refused (:mod:`sa_paths`);
  * test-only backends and providers are refused unless the caller explicitly
    passes ``allow_test_providers=True``, which production entry points never
    do.
"""
import json
import ntpath
import os

import sa_paths

SCHEMA_ID = "saituls.secure-apps/1"
SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

MANAGED_ROOT_TOKEN = "<managed-root>"
MANAGED_ROOT_ENV = "SAITULS_SECURE_APPS_ROOT"
DEFAULT_MANAGED_ROOT = r"%LOCALAPPDATA%\SAITULS\secure-apps"

PRODUCTION_BACKENDS = ("bitlocker-vhdx",)
TEST_BACKENDS = ("fake-memory",)

PRODUCTION_PROVIDERS = ("yubikey-fido2-hmac-secret", "external-helper-fido2-hmac-secret")
TEST_PROVIDERS = ("fake-auth",)

# WebAuthn user-verification policy. The default is REQUIRED on purpose: a
# profile exists to keep data unreadable, and at "discouraged" a stolen key is
# the whole secret. A registry that genuinely wants touch-only must say so.
USER_VERIFICATION_VALUES = ("required", "preferred", "discouraged")
DEFAULT_USER_VERIFICATION = "required"

POLICY_MODES = ("default", "aggressive")

# Typed conditions. No expression language, no scripting: a condition is a
# name from this table plus the typed parameters the table declares.
CONDITION_TYPES = {
    "require_auth_after_windows_lock": {},
    "require_auth_after_suspend": {},
    "require_auth_after_logoff": {},
    "require_auth_after_app_exit": {},
    "require_auth_after_session_expiry": {},
    "require_auth_after_minutes": {"minutes": int},
    "close_app_on_idle": {},
    "unmount_storage_on_app_exit": {},
    "require_auth_provider": {"provider": str},
}

PROFILE_KEYS = {"id", "label", "enabled", "application", "storage",
                "authentication", "policy", "notes"}
APPLICATION_KEYS = {"executable", "working_directory", "arguments",
                    "vault_argument_style", "allow_unmanaged_executable"}
STORAGE_KEYS = {"backend", "container", "container_id", "mount_path",
                "size_gb", "filesystem_label"}
AUTH_KEYS = {"provider", "credential_profile", "helper_executable",
             "user_verification"}
POLICY_KEYS = {"mode", "idle_timeout_minutes", "lock_on_windows_lock",
               "lock_on_suspend", "lock_on_logoff", "lock_on_shutdown",
               "lock_on_broker_shutdown", "unmount_when_app_closes",
               "graceful_close_timeout_seconds", "force_terminate_after_timeout",
               "process_exit_confirm_timeout_seconds", "unmount_timeout_seconds",
               "conditions"}
REGISTRY_KEYS = {"schema", "schema_version", "managed_root", "notes", "profiles"}

VAULT_ARGUMENT_STYLES = ("path", "obsidian-uri", "none")


class ConfigError(ValueError):
    """The registry or one of its profiles is not acceptable. Fail closed."""


def resolve_managed_root(declared=None, registry_value=None):
    """Managed sidecar storage root.

    Precedence: an explicit caller argument, then the
    ``SAITULS_SECURE_APPS_ROOT`` environment override (how tests and a
    portable install relocate it), then the value in the registry, then the
    documented default. The environment deliberately outranks the registry:
    the committed registry names a per-user default, and a relocated install
    must not have to edit tracked source to move its payload. Never the repo.
    """
    for candidate in (declared, os.environ.get(MANAGED_ROOT_ENV), registry_value,
                      DEFAULT_MANAGED_ROOT):
        if candidate:
            return sa_paths.canonical(os.path.expandvars(candidate))
    raise ConfigError("managed root could not be resolved")


def expand(value, managed_root):
    if not isinstance(value, str):
        return value
    out = value.replace(MANAGED_ROOT_TOKEN, managed_root)
    return os.path.expandvars(out)


def _require(mapping, key, where):
    if key not in mapping:
        raise ConfigError("%s is missing required key '%s'" % (where, key))
    return mapping[key]


def _reject_unknown(mapping, allowed, where):
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise ConfigError("%s has unknown key(s): %s" % (where, ", ".join(unknown)))


def _as_bool(value, where, key, default=None):
    if key not in value:
        if default is None:
            raise ConfigError("%s is missing required flag '%s'" % (where, key))
        return default
    raw = value[key]
    if not isinstance(raw, bool):
        raise ConfigError("%s.%s must be a boolean, got %r" % (where, key, raw))
    return raw


def _as_int(value, where, key, default=None, minimum=None, maximum=None):
    if key not in value:
        if default is None:
            raise ConfigError("%s is missing required number '%s'" % (where, key))
        raw = default
    else:
        raw = value[key]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError("%s.%s must be an integer, got %r" % (where, key, raw))
    if minimum is not None and raw < minimum:
        raise ConfigError("%s.%s must be >= %d, got %d" % (where, key, minimum, raw))
    if maximum is not None and raw > maximum:
        raise ConfigError("%s.%s must be <= %d, got %d" % (where, key, maximum, raw))
    return raw


class Condition(object):
    __slots__ = ("type", "params")

    def __init__(self, ctype, params):
        self.type = ctype
        self.params = params

    def __repr__(self):
        return "<Condition %s %r>" % (self.type, self.params)

    def to_dict(self):
        out = {"type": self.type}
        out.update(self.params)
        return out


class Policy(object):
    __slots__ = ("mode", "idle_timeout_minutes", "lock_on_windows_lock",
                 "lock_on_suspend", "lock_on_logoff", "lock_on_shutdown",
                 "lock_on_broker_shutdown", "unmount_when_app_closes",
                 "graceful_close_timeout_seconds", "force_terminate_after_timeout",
                 "process_exit_confirm_timeout_seconds", "unmount_timeout_seconds",
                 "conditions")

    def condition(self, ctype):
        for c in self.conditions:
            if c.type == ctype:
                return c
        return None

    def has(self, ctype):
        return self.condition(ctype) is not None

    def to_dict(self):
        return {
            "mode": self.mode,
            "idle_timeout_minutes": self.idle_timeout_minutes,
            "lock_on_windows_lock": self.lock_on_windows_lock,
            "lock_on_suspend": self.lock_on_suspend,
            "lock_on_logoff": self.lock_on_logoff,
            "lock_on_shutdown": self.lock_on_shutdown,
            "lock_on_broker_shutdown": self.lock_on_broker_shutdown,
            "unmount_when_app_closes": self.unmount_when_app_closes,
            "graceful_close_timeout_seconds": self.graceful_close_timeout_seconds,
            "force_terminate_after_timeout": self.force_terminate_after_timeout,
            "process_exit_confirm_timeout_seconds": self.process_exit_confirm_timeout_seconds,
            "unmount_timeout_seconds": self.unmount_timeout_seconds,
            "conditions": [c.to_dict() for c in self.conditions],
        }


class Profile(object):
    __slots__ = ("id", "label", "enabled", "executable", "working_directory",
                 "arguments", "vault_argument_style", "allow_unmanaged_executable",
                 "backend", "container",
                 "container_id", "mount_path", "size_gb", "filesystem_label",
                 "provider", "credential_profile", "helper_executable",
                 "user_verification", "policy", "managed_root", "notes")

    def with_mount_path(self, mount_path):
        """A shallow copy that mounts somewhere else.

        Migration needs the encrypted destination mounted at a staging path
        while the plaintext original still occupies the final one. Copying the
        profile keeps every other validated field identical, so the staged
        mount goes through exactly the same backend code as the real one.
        """
        clone = Profile()
        for name in Profile.__slots__:
            setattr(clone, name, getattr(self, name))
        clone.mount_path = sa_paths.assert_mount_path(mount_path, "staging mount_path")
        return clone

    def to_dict(self):
        return {
            "id": self.id,
            "label": self.label,
            "enabled": self.enabled,
            "application": {
                "executable": self.executable,
                "working_directory": self.working_directory,
                "arguments": list(self.arguments),
                "vault_argument_style": self.vault_argument_style,
                "allow_unmanaged_executable": self.allow_unmanaged_executable,
            },
            "storage": {
                "backend": self.backend,
                "container": self.container,
                "container_id": self.container_id,
                "mount_path": self.mount_path,
                "size_gb": self.size_gb,
                "filesystem_label": self.filesystem_label,
            },
            "authentication": {
                "provider": self.provider,
                "credential_profile": self.credential_profile,
                "helper_executable": self.helper_executable,
                "user_verification": self.user_verification,
            },
            "policy": self.policy.to_dict(),
        }


class Registry(object):
    def __init__(self, managed_root, profiles, schema_version):
        self.managed_root = managed_root
        self.profiles = profiles
        self.schema_version = schema_version

    @property
    def state_dir(self):
        return ntpath.join(self.managed_root, "state")

    @property
    def credentials_path(self):
        return ntpath.join(self.state_dir, "credentials.json")

    @property
    def state_path(self):
        return ntpath.join(self.state_dir, "secure-apps.json")

    @property
    def audit_path(self):
        return ntpath.join(self.state_dir, "audit.log")

    @property
    def sessions_dir(self):
        return ntpath.join(self.state_dir, "sessions")

    def get(self, profile_id):
        for p in self.profiles:
            if p.id == profile_id:
                return p
        raise ConfigError("unknown profile id: %r" % (profile_id,))

    def ids(self):
        return [p.id for p in self.profiles]


def _parse_conditions(raw, where):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError("%s.conditions must be a list" % where)
    out = []
    seen = set()
    for index, entry in enumerate(raw):
        at = "%s.conditions[%d]" % (where, index)
        if not isinstance(entry, dict):
            raise ConfigError("%s must be an object" % at)
        ctype = entry.get("type")
        if ctype not in CONDITION_TYPES:
            raise ConfigError("%s has unknown condition type %r (allowed: %s)"
                              % (at, ctype, ", ".join(sorted(CONDITION_TYPES))))
        spec = CONDITION_TYPES[ctype]
        _reject_unknown(entry, set(spec) | {"type"}, at)
        params = {}
        for key, expected in spec.items():
            if key not in entry:
                raise ConfigError("%s requires parameter '%s'" % (at, key))
            value = entry[key]
            if expected is int:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ConfigError("%s.%s must be a non-negative integer" % (at, key))
            elif expected is str:
                if not isinstance(value, str) or not value.strip():
                    raise ConfigError("%s.%s must be a non-empty string" % (at, key))
            params[key] = value
        key = (ctype, tuple(sorted(params.items())))
        if key in seen:
            raise ConfigError("%s duplicates an earlier condition" % at)
        seen.add(key)
        out.append(Condition(ctype, params))
    return out


def _parse_policy(raw, where, allowed_providers):
    if not isinstance(raw, dict):
        raise ConfigError("%s.policy must be an object" % where)
    at = where + ".policy"
    _reject_unknown(raw, POLICY_KEYS, at)
    mode = raw.get("mode")
    if mode not in POLICY_MODES:
        raise ConfigError("%s.mode must be one of %s, got %r"
                          % (at, ", ".join(POLICY_MODES), mode))
    p = Policy()
    p.mode = mode
    p.idle_timeout_minutes = _as_int(raw, at, "idle_timeout_minutes",
                                     minimum=1, maximum=7 * 24 * 60)
    p.lock_on_windows_lock = _as_bool(raw, at, "lock_on_windows_lock", default=True)
    p.lock_on_suspend = _as_bool(raw, at, "lock_on_suspend", default=True)
    p.lock_on_logoff = _as_bool(raw, at, "lock_on_logoff", default=True)
    p.lock_on_shutdown = _as_bool(raw, at, "lock_on_shutdown", default=True)
    p.lock_on_broker_shutdown = _as_bool(raw, at, "lock_on_broker_shutdown", default=True)
    p.unmount_when_app_closes = _as_bool(raw, at, "unmount_when_app_closes", default=True)
    p.graceful_close_timeout_seconds = _as_int(
        raw, at, "graceful_close_timeout_seconds", default=30, minimum=0, maximum=3600)
    p.force_terminate_after_timeout = _as_bool(
        raw, at, "force_terminate_after_timeout", default=True)
    p.process_exit_confirm_timeout_seconds = _as_int(
        raw, at, "process_exit_confirm_timeout_seconds", default=20, minimum=1, maximum=3600)
    p.unmount_timeout_seconds = _as_int(
        raw, at, "unmount_timeout_seconds", default=60, minimum=1, maximum=3600)
    p.conditions = _parse_conditions(raw.get("conditions"), at)
    for cond in p.conditions:
        if cond.type == "require_auth_provider":
            wanted = cond.params["provider"]
            if wanted not in allowed_providers:
                raise ConfigError("%s.conditions require_auth_provider names an "
                                  "unavailable provider: %r" % (at, wanted))
    return p


def parse_profile(raw, managed_root, allow_test_providers=False, index=0):
    """Validate one profile object into a :class:`Profile`. Raises ConfigError."""
    where = "profiles[%d]" % index
    if not isinstance(raw, dict):
        raise ConfigError("%s must be an object" % where)
    _reject_unknown(raw, PROFILE_KEYS, where)

    pid = _require(raw, "id", where)
    if not isinstance(pid, str) or not pid.strip():
        raise ConfigError("%s.id must be a non-empty string" % where)
    pid = pid.strip()
    if not all(ch.isalnum() or ch in "-_" for ch in pid):
        raise ConfigError("%s.id may only contain letters, digits, '-' and '_': %r"
                          % (where, pid))
    where = "profile '%s'" % pid

    label = raw.get("label", pid)
    if not isinstance(label, str) or not label.strip():
        raise ConfigError("%s.label must be a non-empty string" % where)

    enabled = _as_bool(raw, where, "enabled", default=True)

    app = _require(raw, "application", where)
    if not isinstance(app, dict):
        raise ConfigError("%s.application must be an object" % where)
    _reject_unknown(app, APPLICATION_KEYS, where + ".application")
    executable = expand(_require(app, "executable", where + ".application"), managed_root)
    if not isinstance(executable, str) or not executable.strip():
        raise ConfigError("%s.application.executable must be a non-empty string" % where)
    working_directory = app.get("working_directory") or ntpath.dirname(executable)
    working_directory = expand(working_directory, managed_root)
    arguments = app.get("arguments", [])
    if not isinstance(arguments, list) or not all(isinstance(a, str) for a in arguments):
        raise ConfigError("%s.application.arguments must be a list of strings" % where)
    style = app.get("vault_argument_style", "path")
    if style not in VAULT_ARGUMENT_STYLES:
        raise ConfigError("%s.application.vault_argument_style must be one of %s"
                          % (where, ", ".join(VAULT_ARGUMENT_STYLES)))
    # The profile owns the executable it launches. By default that means the
    # executable lives inside the managed root -- imported there once, so a
    # change to some other copy on disk cannot silently become what the
    # protected launch runs. Pointing at an unmanaged copy is allowed, but
    # only as an explicit, written-down decision.
    allow_unmanaged = _as_bool(app, where + ".application",
                               "allow_unmanaged_executable", default=False)

    storage = _require(raw, "storage", where)
    if not isinstance(storage, dict):
        raise ConfigError("%s.storage must be an object" % where)
    _reject_unknown(storage, STORAGE_KEYS, where + ".storage")
    backend = _require(storage, "backend", where + ".storage")
    allowed_backends = tuple(PRODUCTION_BACKENDS) + (
        tuple(TEST_BACKENDS) if allow_test_providers else ())
    if backend not in allowed_backends:
        raise ConfigError("%s.storage.backend %r is not available (allowed: %s)"
                          % (where, backend, ", ".join(allowed_backends)))
    container = expand(_require(storage, "container", where + ".storage"), managed_root)
    mount_path = expand(_require(storage, "mount_path", where + ".storage"), managed_root)
    container_id = storage.get("container_id") or pid
    if not isinstance(container_id, str) or not container_id.strip():
        raise ConfigError("%s.storage.container_id must be a non-empty string" % where)
    size_gb = _as_int(storage, where + ".storage", "size_gb", default=8,
                      minimum=1, maximum=4096)
    filesystem_label = storage.get("filesystem_label", "SAITULS-" + pid.upper())
    if not isinstance(filesystem_label, str) or len(filesystem_label) > 32:
        raise ConfigError("%s.storage.filesystem_label must be a string of <=32 chars"
                          % where)

    auth = _require(raw, "authentication", where)
    if not isinstance(auth, dict):
        raise ConfigError("%s.authentication must be an object" % where)
    _reject_unknown(auth, AUTH_KEYS, where + ".authentication")
    provider = _require(auth, "provider", where + ".authentication")
    allowed_providers = tuple(PRODUCTION_PROVIDERS) + (
        tuple(TEST_PROVIDERS) if allow_test_providers else ())
    if provider not in allowed_providers:
        raise ConfigError("%s.authentication.provider %r is not available (allowed: %s)"
                          % (where, provider, ", ".join(allowed_providers)))
    credential_profile = auth.get("credential_profile", "primary")
    if not isinstance(credential_profile, str) or not credential_profile.strip():
        raise ConfigError("%s.authentication.credential_profile must be a non-empty string"
                          % where)
    helper_executable = auth.get("helper_executable")
    if helper_executable is not None:
        if not isinstance(helper_executable, str) or not helper_executable.strip():
            raise ConfigError("%s.authentication.helper_executable must be a non-empty string"
                              % where)
        helper_executable = expand(helper_executable, managed_root)
    user_verification = auth.get("user_verification", DEFAULT_USER_VERIFICATION)
    if user_verification not in USER_VERIFICATION_VALUES:
        raise ConfigError("%s.authentication.user_verification %r is not one of %s"
                          % (where, user_verification,
                             ", ".join(USER_VERIFICATION_VALUES)))
    if provider == "external-helper-fido2-hmac-secret" and not helper_executable:
        raise ConfigError("%s.authentication.provider 'external-helper-fido2-hmac-secret' "
                          "requires helper_executable" % where)

    policy = _parse_policy(_require(raw, "policy", where), where, allowed_providers)

    # --- path policy -----------------------------------------------------
    # Payload paths live under the managed root and may not be redirected out
    # of it by a junction. The mount path is the one declared path that lives
    # outside by design: it is where the protected filesystem appears.
    container = sa_paths.assert_managed(managed_root, container, where + ".storage.container")
    if ntpath.splitext(container)[1].lower() not in (".vhdx", ".vhd", ".img"):
        raise ConfigError("%s.storage.container must be a disk image file, got %s"
                          % (where, container))
    mount_path = sa_paths.assert_mount_path(mount_path, where + ".storage.mount_path")
    if sa_paths.is_within(mount_path, container):
        raise ConfigError("%s.storage.container may not live inside its own mount path"
                          % where)
    if allow_unmanaged:
        # Outside the managed root: containment cannot apply, so the path must
        # at least be absolute and really be a file that exists now.
        executable = sa_paths.canonical(executable)
        if not sa_paths.is_absolute(executable):
            raise ConfigError("%s.application.executable must be absolute: %s"
                              % (where, executable))
        if not os.path.isfile(executable):
            raise ConfigError(
                "%s.application.executable declares allow_unmanaged_executable "
                "but no file exists at %s" % (where, executable))
        working_directory = sa_paths.canonical(working_directory)
    else:
        executable = sa_paths.assert_within(managed_root, executable,
                                            where + ".application.executable")
        executable = sa_paths.assert_no_reparse_below(
            managed_root, executable, where + ".application.executable")
        working_directory = sa_paths.assert_managed(
            managed_root, working_directory,
            where + ".application.working_directory")
    if helper_executable:
        helper_executable = sa_paths.canonical(helper_executable)

    p = Profile()
    p.id = pid
    p.label = label.strip()
    p.enabled = enabled
    p.executable = executable
    p.working_directory = working_directory
    p.arguments = list(arguments)
    p.vault_argument_style = style
    p.allow_unmanaged_executable = allow_unmanaged
    p.backend = backend
    p.container = container
    p.container_id = container_id.strip()
    p.mount_path = mount_path
    p.size_gb = size_gb
    p.filesystem_label = filesystem_label
    p.provider = provider
    p.credential_profile = credential_profile.strip()
    p.helper_executable = helper_executable
    p.user_verification = user_verification
    p.policy = policy
    p.managed_root = managed_root
    p.notes = raw.get("notes")
    return p


def parse_registry(document, managed_root=None, allow_test_providers=False):
    """Validate a decoded registry document into a :class:`Registry`."""
    if not isinstance(document, dict):
        raise ConfigError("registry must be a JSON object")
    _reject_unknown(document, REGISTRY_KEYS, "registry")

    schema = document.get("schema")
    if schema != SCHEMA_ID:
        raise ConfigError("unsupported registry schema %r (expected %r)"
                          % (schema, SCHEMA_ID))
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigError("registry schema_version must be an integer, got %r" % (version,))
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ConfigError("unsupported registry schema_version %d (supported: %s)"
                          % (version, ", ".join(str(v) for v in SUPPORTED_SCHEMA_VERSIONS)))

    root = resolve_managed_root(managed_root, document.get("managed_root"))

    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ConfigError("registry must declare a non-empty 'profiles' list")
    profiles = []
    seen = set()
    for index, raw in enumerate(raw_profiles):
        profile = parse_profile(raw, root, allow_test_providers=allow_test_providers,
                                index=index)
        if profile.id in seen:
            raise ConfigError("duplicate profile id: %r" % (profile.id,))
        seen.add(profile.id)
        profiles.append(profile)
    return Registry(root, profiles, version)


def load_registry(path, managed_root=None, allow_test_providers=False):
    """Read and validate ``secure_apps.json``. Any problem raises ConfigError."""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise ConfigError("registry not found: %s" % path)
    except json.JSONDecodeError as exc:
        raise ConfigError("registry is not valid JSON (%s): %s" % (exc, path))
    return parse_registry(document, managed_root=managed_root,
                          allow_test_providers=allow_test_providers)


def default_registry_path():
    return ntpath.join(ntpath.dirname(ntpath.abspath(__file__)), "secure_apps.json")
