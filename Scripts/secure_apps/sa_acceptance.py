"""Durable hardware-acceptance gate for SAITULS Secure Apps migration.

Migrating a real vault is the one operation whose failure modes cannot be
rehearsed with fakes alone: it needs the physical authenticator, the elevated
CTAP helper, BitLocker and the lock policy to behave on THIS host. The
disposable acceptance run (``tests/secure_apps_interactive.py
--disposable-acceptance``) proves that against a throwaway container and
writes the record this module reads.

``sa_cli.py migrate`` refuses to start unless :func:`evaluate` accepts that
record for the implementation and environment that are running *now*::

    FIDO2_HARDWARE_ACCEPTED    STORAGE_ACCEPTED    MIGRATION_READY

A record is stale -- and refused -- as soon as anything it vouched for has
changed:

* the acceptance schema;
* the host (machine identity) and the integrity level the broker runs at;
* the silent elevation mechanism: the launch mode, the task name, the
  fingerprint of the scheduled-task *definition* a medium-integrity broker may
  start without a consent dialog, the fingerprint of that task's *security
  descriptor* (who may run vs modify vs delete it) and the fingerprint of the
  protected *runtime bundle* (the installed helper, the frozen worker and the
  task action path). A record proves that *that* fixed action, protected by
  *that* task ACL, running *that* installed runtime, produced a high-integrity
  helper; change any of the three and the proof is about something else;
* the profile's authentication provider, user-verification policy or storage
  backend -- and, as one canonical fingerprint, every other security-relevant
  field of the profile a migration would use: mount path, container, the
  launched application, and the whole lock policy with its typed conditions;
* the transport this host would really use (a ``webauthn.dll`` that learns to
  carry an hmac-secret salt moves the host from ELEVATED_CTAP_HELPER to
  WINDOWS_WEBAUTHN) and the hmac-secret transport semantics;
* the python-fido2 major version;
* the source of any module in :data:`SECURITY_SOURCES`;
* the source of the acceptance runner itself
  (:data:`ACCEPTANCE_PRODUCER_SOURCES`). The program that decides whether
  F01..X02 passed -- and therefore whether MIGRATION_READY is written -- is
  as security-critical as the broker it exercises, so a changed, weakened or
  unreadable runner invalidates the record it wrote.

The broker's integrity level is not merely *matched*, it is *required*: both
the recorded run and the process asking to migrate must be medium integrity
(``broker_elevated is False``). An elevated broker never produces an
acceptable record and never consumes one. Only the narrow storage/FIDO helper
boundary is elevated.

The record is public facts and booleans, built from :data:`RECORD_FIELDS`
and nothing else -- the allowlist shape of :mod:`sa_audit` -- so a PIN, a
credential secret, an hmac-secret output, a BitLocker password, recovery
material or wrapped-key plaintext has no code path into it.
"""
import hashlib
import json
import os
import platform
import sys
import time

import sa_auth
import sa_privtask

SCHEMA = "saituls.secure-apps.hardware-acceptance/4"
SCHEMA_VERSION = 4
RECORD_FILENAME = "hardware-acceptance.json"

TOKEN_FIDO2_HARDWARE_ACCEPTED = "FIDO2_HARDWARE_ACCEPTED"
TOKEN_STORAGE_ACCEPTED = "STORAGE_ACCEPTED"
TOKEN_MIGRATION_READY = "MIGRATION_READY"
FINAL_TOKENS = (TOKEN_FIDO2_HARDWARE_ACCEPTED, TOKEN_STORAGE_ACCEPTED,
                TOKEN_MIGRATION_READY)
MIGRATION_BLOCKED = "MIGRATION_BLOCKED: HARDWARE_ACCEPTANCE_REQUIRED"
BROKER_MUST_RUN_MEDIUM_INTEGRITY = "BROKER_MUST_RUN_MEDIUM_INTEGRITY"

#: Every property the disposable run must prove. MIGRATION_READY is the
#: conjunction of all of them and is never recorded on its own authority.
GATE_FLAGS = (
    "fido2_hardware_accepted",
    "storage_accepted",
    "silent_privileged_start_accepted",
    "privileged_task_security_accepted",
    "privileged_runtime_acl_accepted",
    "default_mode_accepted",
    "aggressive_mode_accepted",
    "workstation_lock_accepted",
    "helper_failure_accepted",
    "audit_hygiene_accepted",
)

#: Facts about the running implementation and host. None of them needs an
#: authenticator, elevation or a mount to measure.
#:
#: The three ``privileged_*`` fields bind the record to the *elevation
#: mechanism* as well as to the code: an acceptance run proves that a
#: medium-integrity broker could start a high-integrity helper silently
#: through one particular fixed task definition, and that proof expires the
#: moment that definition changes.
ENVIRONMENT_FIELDS = (
    "host_fingerprint",
    "windows_build",
    "uac_enabled",
    "broker_elevated",
    "privileged_launch_mode",
    "privileged_task_name",
    "privileged_task_definition_fingerprint",
    "privileged_task_security_fingerprint",
    "privileged_runtime_bundle_fingerprint",
    "webauthn_available",
    "webauthn_api_version",
    "python_version",
    "python_fido2_version",
    "python_fido2_major",
    "transport",
    "hmac_transport_semantics",
    "implementation_fingerprint",
    "acceptance_producer_fingerprint",
)

#: What the record says about the profile a migration would use. The three
#: named fields stay for diagnostics -- a refusal that says "storage backend
#: changed" is worth more than one that says "a hash changed" -- but
#: ``profile_security_fingerprint`` is what closes the hole: it covers every
#: security-relevant field, so no policy edit survives it.
PROFILE_FIELDS = ("provider", "user_verification_policy", "storage_backend",
                  "profile_security_fingerprint")

RECORD_FIELDS = (("schema", "schema_version", "timestamp") + ENVIRONMENT_FIELDS
                 + PROFILE_FIELDS + GATE_FLAGS + ("migration_ready",))

#: The production sources whose behaviour the acceptance run vouches for:
#: everything the migration entry point imports, plus the elevated helper
#: script and the FIDO worker it launches. Editing any of them invalidates an
#: existing record. tests/test_secure_apps_acceptance.py fails when the CLI
#: grows an import that is missing here. sa_ctap.py, sa_hid.py and sa_cbor.py
#: are deliberately absent: nothing in production imports them.
SECURITY_SOURCES = (
    "sa_acceptance.py",
    "sa_applife.py",
    "sa_audit.py",
    "sa_auth.py",
    "sa_broker.py",
    "sa_cli.py",
    "sa_config.py",
    "sa_crypto.py",
    "sa_enroll.py",
    "sa_fido_worker.py",
    "sa_migrate.py",
    "sa_paths.py",
    "sa_privhelper.py",
    "sa_privtask.py",
    "sa_state.py",
    "sa_storage.py",
    "sa_storage_helper.ps1",
)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
ACCEPTANCE_SCRIPT = os.path.join(REPO_ROOT, "tests", "secure_apps_interactive.py")

#: The acceptance *producer*: the program that runs the checks, decides
#: whether each passed and aggregates them into the gate flags. Repo-relative,
#: "/"-separated. If those check definitions or that pass/fail aggregation are
#: ever extracted into their own module, add it here -- this fingerprint must
#: cover wherever the logic lives, not merely the file it started in.
ACCEPTANCE_PRODUCER_SOURCES = ("tests/secure_apps_interactive.py",
                               "tests/sa_privboundary_probe.py",
                               "Scripts/secure_apps/sa_acceptance_runner.py")

MAX_TEXT = 128


class AcceptanceError(Exception):
    """A record could not be built or persisted. Never carries a secret."""


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------
def _hklm_value(subkey, name):
    if os.name != "nt":
        return None
    try:
        import winreg
        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey, 0, access) as key:
            value, _kind = winreg.QueryValueEx(key, name)
        return value
    except OSError:
        return None


def host_fingerprint():
    """SHA-256 over the machine GUID and host name, or None when unreadable.

    A record copied to another machine, or left behind by a reinstall, is
    not evidence about this one.
    """
    guid = str(_hklm_value(r"SOFTWARE\Microsoft\Cryptography", "MachineGuid")
               or "").strip().lower()
    if not guid:
        return None
    node = (platform.node() or "").strip().upper()
    text = "saituls.secure-apps.host/1|%s|%s" % (guid, node)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def windows_build():
    """``major.minor.build.ubr``, e.g. ``10.0.19045.6332``. Recorded, not gated."""
    if os.name != "nt":
        return None
    try:
        version = sys.getwindowsversion()
        text = "%d.%d.%d" % (version.major, version.minor, version.build)
    except Exception:
        return None
    ubr = _hklm_value(r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "UBR")
    if isinstance(ubr, int):
        text += ".%d" % ubr
    return text


def uac_enabled():
    """EnableLUA. False means every process of an administrator is elevated."""
    value = _hklm_value(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
                        "EnableLUA")
    if value is None:
        return None
    return bool(value)


def fido2_facts():
    """``(version, major, webauthn_available, webauthn_api_version)``.

    The same imports the provider uses. No authenticator is touched.
    """
    try:
        from importlib import metadata
        version = metadata.version("fido2")
    except Exception:
        version = None
    major = sa_auth.parse_library_version(version)[0] if version else None
    available, api = False, 0
    try:
        from fido2.client.windows import WindowsClient
        try:
            from fido2.client.win_api import WEBAUTHN_API_VERSION
        except ImportError:                                 # pragma: no cover
            from fido2.client.windows import WEBAUTHN_API_VERSION
        available = bool(WindowsClient.is_available())
        api = int(WEBAUTHN_API_VERSION)
    except Exception:
        available, api = False, 0
    return version, major, available, api


def expected_transport(webauthn_available, webauthn_api_version, windows=None):
    """The transport the provider really selects on such a host.

    Mirrors :meth:`sa_auth.Fido2HmacSecretProvider.resolve_transport` without
    touching an authenticator: WindowsClient only where the platform API can
    carry an hmac-secret salt, otherwise the elevated CTAP helper the broker
    always wires on Windows.
    """
    windows = (os.name == "nt") if windows is None else bool(windows)
    if (webauthn_available and int(webauthn_api_version or 0)
            >= sa_auth.WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION):
        return sa_auth.TRANSPORT_WINDOWS_WEBAUTHN
    if windows:
        return sa_auth.TRANSPORT_ELEVATED_CTAP_HELPER
    return sa_auth.TRANSPORT_DIRECT_CTAP


def _source_digest(domain, root, sources):
    """SHA-256 over *sources* under *root*, line endings normalised.

    Each file is bound to its own name and length before its bytes, so no
    rearrangement of content between two sources can produce the same digest.
    None when any source is missing or unreadable: something that cannot be
    measured cannot be matched against a record.
    """
    digest = hashlib.sha256(domain)
    for name in sources:
        try:
            with open(os.path.join(root, *name.split("/")), "rb") as handle:
                content = handle.read()
        except OSError:
            return None
        content = content.replace(b"\r\n", b"\n")
        digest.update(b"\0" + name.encode("ascii") + b"\0")
        digest.update(str(len(content)).encode("ascii") + b"\0")
        digest.update(content)
    return digest.hexdigest()


def implementation_fingerprint(source_dir=None, sources=SECURITY_SOURCES):
    """SHA-256 over :data:`SECURITY_SOURCES`, line endings normalised.

    None when a source is missing: an implementation that cannot be measured
    cannot be matched against a record.
    """
    return _source_digest(b"saituls.secure-apps.implementation/1",
                          source_dir or HERE, sources)


def acceptance_producer_fingerprint(repo_root=None,
                                    sources=ACCEPTANCE_PRODUCER_SOURCES):
    """SHA-256 over :data:`ACCEPTANCE_PRODUCER_SOURCES`.

    The acceptance runner is the program whose verdict the record *is*: it
    decides whether F01..X02 passed and therefore whether MIGRATION_READY is
    written at all. Weakening a check there would otherwise be invisible to a
    gate that fingerprints only the code under test. None when the producer is
    missing or unreadable, which refuses every record exactly as a change does.
    """
    return _source_digest(b"saituls.secure-apps.acceptance-producer/1",
                          repo_root or REPO_ROOT, sources)


def privileged_launch_facts():
    """The silent elevation mechanism, as it stands on this host right now.

    ``privileged_task_definition_fingerprint`` is measured from the task that
    is actually registered, not from the pin -- a record must expire when the
    *installed* definition moves, which is precisely the case the pin cannot
    report on its own.
    """
    facts = {
        "privileged_launch_mode": sa_privtask.LAUNCH_MODE,
        "privileged_task_name": sa_privtask.TASK_PATH,
        "privileged_task_definition_fingerprint": None,
        "privileged_task_security_fingerprint": None,
        "privileged_runtime_bundle_fingerprint": None,
    }
    if os.name != "nt":
        return facts
    try:
        pin, _problem = sa_privtask.load_pin()
        if pin:
            facts["privileged_task_name"] = pin.get("task_path") or sa_privtask.TASK_PATH
            facts["privileged_launch_mode"] = (pin.get("launch_mode")
                                               or sa_privtask.LAUNCH_MODE)
            # The bundle fingerprint is a public fact of the installed runtime;
            # the pin records the one accepted at installation.
            facts["privileged_runtime_bundle_fingerprint"] = \
                pin.get("runtime_bundle_fingerprint")
        facts["privileged_task_definition_fingerprint"] = \
            sa_privtask.installed_fingerprint()
        # Measured from the task that is actually registered, like the
        # definition fingerprint -- so a record expires when the *installed*
        # task security descriptor moves, not merely when the pin does.
        facts["privileged_task_security_fingerprint"] = \
            sa_privtask.installed_task_security_fingerprint()
    except Exception:
        pass
    return facts


def probe_environment():
    """Everything a record is matched against, measured now."""
    version, major, available, api = fido2_facts()
    privileged = privileged_launch_facts()
    return {
        "host_fingerprint": host_fingerprint(),
        "windows_build": windows_build(),
        "uac_enabled": uac_enabled(),
        "broker_elevated": sa_auth._process_is_elevated(),
        "privileged_launch_mode": privileged["privileged_launch_mode"],
        "privileged_task_name": privileged["privileged_task_name"],
        "privileged_task_definition_fingerprint":
            privileged.get("privileged_task_definition_fingerprint"),
        "privileged_task_security_fingerprint":
            privileged.get("privileged_task_security_fingerprint"),
        "privileged_runtime_bundle_fingerprint":
            privileged.get("privileged_runtime_bundle_fingerprint"),
        "webauthn_available": bool(available),
        "webauthn_api_version": int(api),
        "python_version": sys.version.split()[0],
        "python_fido2_version": version,
        "python_fido2_major": major,
        "transport": expected_transport(available, api),
        "hmac_transport_semantics": sa_auth.DEFAULT_HMAC_TRANSPORT_SEMANTICS,
        "implementation_fingerprint": implementation_fingerprint(),
        "acceptance_producer_fingerprint": acceptance_producer_fingerprint(),
    }


# --------------------------------------------------------------------------
# record
# --------------------------------------------------------------------------
def _scalar(value):
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    return str(value)[:MAX_TEXT]


#: Cosmetic profile data, deliberately outside the security fingerprint: a
#: renamed label or a rewritten note must not cost a physical-key acceptance
#: run. ``size_gb`` and ``filesystem_label`` are here for the same reason --
#: they size and name a container whose creation acceptance already proved,
#: and neither decides who may open it, where its plaintext lands, or when it
#: relocks.
PROFILE_COSMETIC_FIELDS = ("label", "notes", "size_gb", "filesystem_label")

PROFILE_SECURITY_SCHEMA = "saituls.secure-apps.profile-security/1"


def canonical_json(document):
    """One byte sequence per value: sorted keys, no insignificant whitespace."""
    return json.dumps(document, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _canonical_conditions(conditions):
    """Typed conditions and their parameters, in a stable order.

    ``sa_config`` rejects a duplicate ``(type, params)``, so a profile's
    conditions are a set and sorting their canonical form loses nothing --
    while reordering the same conditions in the registry file stops looking
    like a policy change.
    """
    items = [dict(condition.to_dict()) for condition in conditions or ()]
    return sorted(items, key=canonical_json)


def profile_security_document(profile):
    """Every security-relevant field of *profile*, and nothing cosmetic.

    What belongs here is what decides **who may open the vault, after what
    verification, where its plaintext appears, which program touches it and
    when it relocks**; :data:`PROFILE_COSMETIC_FIELDS` is what deliberately
    does not. No secret can reach it -- a profile holds none.
    """
    policy = profile.policy
    return {
        "schema": PROFILE_SECURITY_SCHEMA,
        "id": profile.id,
        "enabled": bool(profile.enabled),
        "authentication": {
            "provider": profile.provider,
            "credential_profile": profile.credential_profile,
            "helper_executable": profile.helper_executable,
            "user_verification": profile.user_verification,
        },
        "storage": {
            "backend": profile.backend,
            "container": profile.container,
            "container_id": profile.container_id,
            "mount_path": profile.mount_path,
        },
        "application": {
            "executable": profile.executable,
            "working_directory": profile.working_directory,
            "arguments": list(profile.arguments or ()),
            "vault_argument_style": profile.vault_argument_style,
            "allow_unmanaged_executable": bool(profile.allow_unmanaged_executable),
        },
        "policy": {
            "mode": policy.mode,
            "idle_timeout_minutes": policy.idle_timeout_minutes,
            "lock_on_windows_lock": bool(policy.lock_on_windows_lock),
            "lock_on_suspend": bool(policy.lock_on_suspend),
            "lock_on_logoff": bool(policy.lock_on_logoff),
            "lock_on_shutdown": bool(policy.lock_on_shutdown),
            "lock_on_broker_shutdown": bool(policy.lock_on_broker_shutdown),
            "unmount_when_app_closes": bool(policy.unmount_when_app_closes),
            "graceful_close_timeout_seconds": policy.graceful_close_timeout_seconds,
            "force_terminate_after_timeout": bool(policy.force_terminate_after_timeout),
            "process_exit_confirm_timeout_seconds":
                policy.process_exit_confirm_timeout_seconds,
            "unmount_timeout_seconds": policy.unmount_timeout_seconds,
            "conditions": _canonical_conditions(policy.conditions),
        },
    }


def profile_security_fingerprint(profile):
    """SHA-256 over :func:`profile_security_document`, canonically serialised.

    Only the digest is persisted. None when the profile cannot be measured,
    which refuses every record rather than matching one by accident.
    """
    if profile is None:
        return None
    try:
        payload = canonical_json(profile_security_document(profile))
    except (AttributeError, TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def profile_facts(profile):
    return {
        "provider": profile.provider,
        "user_verification_policy": profile.user_verification,
        "storage_backend": profile.backend,
        "profile_security_fingerprint": profile_security_fingerprint(profile),
    }


def build_record(environment, profile, gates, complete=True, timestamp=None):
    """The only way a record is made.

    *profile* is a :class:`sa_config.Profile` or a dict of
    :data:`PROFILE_FIELDS`. A gate counts only when its value is exactly
    ``True``. ``migration_ready`` is derived, never passed in: it is True only
    when every gate is True, the run reached its end (*complete*), and the
    broker that ran it was medium integrity. An elevated run proves the
    architecture Secure Apps is built on was not in force, so it can never
    grant readiness, however green its checks were. Anything outside
    :data:`RECORD_FIELDS` is dropped.
    """
    environment = environment or {}
    facts = profile if isinstance(profile, dict) else profile_facts(profile)
    gates = gates or {}
    record = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    for name in ENVIRONMENT_FIELDS:
        record[name] = _scalar(environment.get(name))
    for name in PROFILE_FIELDS:
        record[name] = _scalar(facts.get(name))
    for name in GATE_FLAGS:
        record[name] = gates.get(name) is True
    record["migration_ready"] = bool(
        complete is True and record["broker_elevated"] is False
        and all(record[name] for name in GATE_FLAGS))
    return {name: record[name] for name in RECORD_FIELDS}


def record_path(state_dir):
    return os.path.join(state_dir, RECORD_FILENAME)


def save_record(state_dir, record):
    """Atomically replace the record. Refuses any field outside the allowlist."""
    unknown = sorted(set(record) - set(RECORD_FIELDS))
    if unknown:
        raise AcceptanceError("refusing to persist field(s) outside the acceptance "
                              "allowlist: %s" % ", ".join(unknown))
    document = {name: record[name] for name in RECORD_FIELDS if name in record}
    os.makedirs(state_dir, exist_ok=True)
    path = record_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def revoke(state_dir):
    """Remove the record. True when none is left behind."""
    try:
        os.remove(record_path(state_dir))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def load_record(state_dir):
    """``(record, problem)``. Exactly one of the two is None."""
    path = record_path(state_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None, "no hardware acceptance record exists at %s" % path
    except (OSError, ValueError):
        return None, ("the hardware acceptance record at %s is unreadable or "
                      "malformed" % path)
    if not isinstance(document, dict):
        return None, "the hardware acceptance record at %s is not a JSON object" % path
    return document, None


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------
class AcceptanceVerdict(object):
    __slots__ = ("ok", "reasons", "record", "path")

    def __init__(self, ok, reasons, record, path):
        self.ok = bool(ok)
        self.reasons = list(reasons)
        self.record = record
        self.path = path

    def to_dict(self):
        record = self.record if isinstance(self.record, dict) else {}
        return {
            "ok": self.ok,
            "status": (list(FINAL_TOKENS) if self.ok else MIGRATION_BLOCKED),
            "record_path": self.path,
            "reasons": list(self.reasons),
            "accepted_at": record.get("timestamp"),
            "transport": record.get("transport"),
            "provider": record.get("provider"),
            "user_verification_policy": record.get("user_verification_policy"),
            "python_fido2_version": record.get("python_fido2_version"),
            "windows_build": record.get("windows_build"),
            "broker_elevated": record.get("broker_elevated"),
            "privileged_launch_mode": record.get("privileged_launch_mode"),
            "privileged_task_name": record.get("privileged_task_name"),
            "privileged_task_definition_fingerprint":
                _short(record.get("privileged_task_definition_fingerprint")),
            "privileged_task_security_fingerprint":
                _short(record.get("privileged_task_security_fingerprint")),
            "privileged_runtime_bundle_fingerprint":
                _short(record.get("privileged_runtime_bundle_fingerprint")),
            "host_fingerprint": _short(record.get("host_fingerprint")),
            "implementation_fingerprint": _short(record.get("implementation_fingerprint")),
            "acceptance_producer_fingerprint":
                _short(record.get("acceptance_producer_fingerprint")),
            "profile_security_fingerprint":
                _short(record.get("profile_security_fingerprint")),
        }


def _short(value):
    return (str(value)[:16] + "...") if value else value


def _compare(reasons, label, recorded, current, show=True):
    if current is None:
        reasons.append("the current %s cannot be determined, so no record can "
                       "match it" % label)
    elif recorded != current:
        if show:
            reasons.append("%s changed since acceptance (recorded %r, now %r)"
                           % (label, recorded, current))
        else:
            reasons.append("%s changed since acceptance (recorded %s, now %s)"
                           % (label, _short(recorded), _short(current)))


def evaluate(state_dir, profile, environment=None):
    """Does the durable record authorise migrating *profile* right now?

    Fails closed on every doubt and reports every reason, not the first. The
    record must have been produced by this acceptance runner, on this host,
    by a medium-integrity broker, for a profile whose security-relevant
    configuration has not moved since -- and this process must be medium
    integrity too.
    """
    path = record_path(state_dir)
    record, problem = load_record(state_dir)
    if record is None:
        return AcceptanceVerdict(False, [problem], None, path)
    env = probe_environment() if environment is None else environment
    reasons = []

    unknown = sorted(set(record) - set(RECORD_FIELDS))
    if unknown:
        reasons.append("the record carries field(s) outside the acceptance "
                       "allowlist: %s" % ", ".join(unknown))
    if record.get("schema") != SCHEMA or record.get("schema_version") != SCHEMA_VERSION:
        reasons.append("the record uses acceptance schema %r version %r; this "
                       "implementation requires %r version %d"
                       % (record.get("schema"), record.get("schema_version"),
                          SCHEMA, SCHEMA_VERSION))
    for name in GATE_FLAGS:
        if record.get(name) is not True:
            reasons.append("%s was not accepted by the recorded run" % name)
    if record.get("migration_ready") is not True:
        reasons.append("the recorded run did not grant migration readiness")

    # Medium integrity is a requirement, not a property to match. Equal
    # values would otherwise let an elevated acceptance authorise an elevated
    # migration, which is exactly the architecture this gate exists to keep.
    if record.get("broker_elevated") is not False:
        reasons.append("the recorded run did not prove a medium-integrity broker "
                       "(broker_elevated %r): %s"
                       % (record.get("broker_elevated"),
                          BROKER_MUST_RUN_MEDIUM_INTEGRITY))
    if env.get("broker_elevated") is not False:
        reasons.append("this process is elevated, or its integrity level cannot be "
                       "determined (broker_elevated %r): %s"
                       % (env.get("broker_elevated"),
                          BROKER_MUST_RUN_MEDIUM_INTEGRITY))

    # The elevation mechanism is part of what was accepted. A record made
    # against one fixed task definition says nothing about another one, so a
    # changed, removed or unreadable task definition invalidates readiness
    # exactly as a changed implementation does.
    _compare(reasons, "privileged launch mode", record.get("privileged_launch_mode"),
             env.get("privileged_launch_mode"))
    _compare(reasons, "privileged helper task", record.get("privileged_task_name"),
             env.get("privileged_task_name"))
    _compare(reasons, "privileged task definition",
             record.get("privileged_task_definition_fingerprint"),
             env.get("privileged_task_definition_fingerprint"), show=False)
    _compare(reasons, "privileged task security descriptor",
             record.get("privileged_task_security_fingerprint"),
             env.get("privileged_task_security_fingerprint"), show=False)
    _compare(reasons, "privileged runtime bundle",
             record.get("privileged_runtime_bundle_fingerprint"),
             env.get("privileged_runtime_bundle_fingerprint"), show=False)

    _compare(reasons, "host identity", record.get("host_fingerprint"),
             env.get("host_fingerprint"), show=False)
    _compare(reasons, "broker integrity (elevated)", record.get("broker_elevated"),
             env.get("broker_elevated"))
    _compare(reasons, "transport", record.get("transport"), env.get("transport"))
    _compare(reasons, "hmac-secret transport semantics",
             record.get("hmac_transport_semantics"),
             env.get("hmac_transport_semantics"))
    _compare(reasons, "python-fido2 major version", record.get("python_fido2_major"),
             env.get("python_fido2_major"))
    _compare(reasons, "Secure Apps implementation",
             record.get("implementation_fingerprint"),
             env.get("implementation_fingerprint"), show=False)
    _compare(reasons, "acceptance producer",
             record.get("acceptance_producer_fingerprint"),
             env.get("acceptance_producer_fingerprint"), show=False)

    facts = profile_facts(profile)
    _compare(reasons, "authentication provider", record.get("provider"),
             facts["provider"])
    _compare(reasons, "user verification policy",
             record.get("user_verification_policy"),
             facts["user_verification_policy"])
    _compare(reasons, "storage backend", record.get("storage_backend"),
             facts["storage_backend"])
    _compare(reasons, "security-relevant profile configuration",
             record.get("profile_security_fingerprint"),
             facts["profile_security_fingerprint"], show=False)
    return AcceptanceVerdict(not reasons, reasons, record, path)


def acceptance_command(profile_id=None, registry_path=None, managed_root=None,
                       python=None):
    """The exact command that produces a record this gate can accept."""
    parts = ['"%s"' % (python or sys.executable), '"%s"' % ACCEPTANCE_SCRIPT,
             "--disposable-acceptance"]
    if profile_id:
        parts += ["--profile", profile_id]
    if registry_path:
        parts += ["--registry", '"%s"' % registry_path]
    if managed_root:
        parts += ["--managed-root", '"%s"' % managed_root]
    return " ".join(parts)
