# Privilege boundary probe: probe.run_medium is called during acceptance
#!/usr/bin/env python
"""INTERACTIVE hardware acceptance for SAITULS Secure Apps.

NOT a CI test. The hardware modes need a physical FIDO2 authenticator, an
elevated storage helper, and a human to type a PIN, touch the key, unplug it
and lock the workstation. No workflow runs them, on purpose: a pipeline that
requires a YubiKey is a pipeline that goes red when somebody unplugs one.
``tests/test_secure_apps_acceptance.py`` imports this module only for its
hardware-free building blocks (the disposable registry, the gate bookkeeping,
the audit scanner) and never starts a hardware mode.

Run it deliberately:

    python tests\\secure_apps_interactive.py --probe
    python tests\\secure_apps_interactive.py --disposable-acceptance
    python tests\\secure_apps_interactive.py --enroll    --profile obsidian
    python tests\\secure_apps_interactive.py --unlock    --profile obsidian
    python tests\\secure_apps_interactive.py --lifecycle --profile obsidian

``--probe`` touches nothing: it reports whether the FIDO2 stack, the
authenticator and the hmac-secret extension are present, and whether the
migration gate would accept the current acceptance record.

``--disposable-acceptance`` is the migration gate. Against a throwaway 1 GiB
BitLocker container -- never a configured vault -- it proves every property
the real migration depends on (see :data:`CHECKS`), and only when every
single check passed does it write ``state\\hardware-acceptance.json`` for the
production registry and print the three acceptance tokens. A skipped,
cancelled, unavailable or failed check leaves a record that authorises
nothing. ``sa_cli.py migrate`` refuses to run without a matching record.

It refuses to start at all from an elevated process. Secure Apps is built on
a medium-integrity broker with one narrow elevated helper; an acceptance run
that was itself elevated proves nothing about that architecture, so it prints
``FAIL: BROKER_MUST_RUN_MEDIUM_INTEGRITY`` and creates neither an enrollment
nor a disposable container. Run it from an ordinary shell; Windows asks for
the helper's elevation when it is needed.

This file is itself fingerprinted into the record it writes
(``sa_acceptance.ACCEPTANCE_PRODUCER_SOURCES``): the program that decides
whether a check passed is as security-critical as the code it exercises, so
editing it invalidates every record it produced.

Nothing here prints a secret. Every mode writes a non-secret EVIDENCE RECORD
built from :data:`EVIDENCE_FIELDS` and nothing else -- an allowlist, like
:mod:`sa_audit`, because the reliable way to keep a PIN, an hmac-secret
output, a volume password, BitLocker recovery material or a wrapped key out
of a file is to never have a code path that could put one there.
"""
import argparse
import hashlib
import json
import ntpath
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_acceptance      # noqa: E402
import sa_audit           # noqa: E402
import sa_auth            # noqa: E402  (transport + version constants)
import sa_broker          # noqa: E402
import sa_config          # noqa: E402
import sa_enroll          # noqa: E402
import sa_paths           # noqa: E402
import sa_privtask        # noqa: E402
import sa_state           # noqa: E402
import sa_storage         # noqa: E402

import sa_privboundary_probe as probe   # noqa: E402  (tests/, real access probes)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
_results = []

#: The complete, closed set of facts an evidence record may carry. Anything
#: not named here cannot reach the evidence file, whatever a caller passes.
#: Values are coerced to short scalars.
EVIDENCE_FIELDS = (
    "timestamp",
    "run_id",
    "profile_id",
    "python",
    "python_fido2_version",
    "provider",
    "transport",
    "windows_build",
    "uac_enabled",
    "broker_elevated",
    "broker_medium_integrity",
    "acceptance_producer_fingerprint",
    "profile_security_fingerprint",
    "windows_webauthn_available",
    "windows_webauthn_api_version",
    "authenticator_detected",
    "hmac_secret_available",
    "user_verification_available",
    "user_verification_policy",
    "enrolled_keys",
    "enrollment",
    "unlock",
    "default_reopen",
    "aggressive_reopen_requires_auth",
    "workstation_lock_relock",
    "vault_detached",
    "audit_log_clean",
    "mode",
    "migration_gate",
    "checks_passed",
    "checks_failed",
    "checks_skipped",
    "checks_missing",
    "fido2_hardware_accepted",
    "storage_accepted",
    "default_mode_accepted",
    "aggressive_mode_accepted",
    "workstation_lock_accepted",
    "helper_failure_accepted",
    "audit_hygiene_accepted",
    "migration_ready",
)

_evidence = {}


def evidence(name, value):
    """Record one allowlisted fact. Silently drops anything else."""
    if name not in EVIDENCE_FIELDS:
        return None
    if isinstance(value, bool) or isinstance(value, int) or value is None:
        _evidence[name] = value
    else:
        _evidence[name] = str(value)[:200]
    return _evidence[name]


def write_evidence(path):
    """Write the record, in EVIDENCE_FIELDS order. Returns the path."""
    record = {name: _evidence[name] for name in EVIDENCE_FIELDS
              if name in _evidence}
    record.setdefault("timestamp",
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        directory = ntpath.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, indent=2, sort_keys=False)
            handle.write("\n")
    except OSError as exc:
        print("  could not write the evidence record: %s" % type(exc).__name__)
        return None
    print("")
    print("Evidence record (non-secret): %s" % path)
    print(json.dumps(record, indent=2))
    return path


def verdict(name, ok):
    """PASS / FAIL as an evidence value, mirroring report()."""
    return evidence(name, PASS if ok else FAIL)


def fido2_facts():
    """Version and transport, straight from the provider. No hardware needed."""
    version, _major, available, api = sa_acceptance.fido2_facts()
    evidence("python_fido2_version", version)
    evidence("windows_webauthn_available", available)
    evidence("windows_webauthn_api_version", api)
    return available, api


def report(name, ok, detail=""):
    outcome = PASS if ok else FAIL
    _results.append((outcome, name, detail))
    print("  %-4s %s%s" % (outcome, name, ("  " + detail) if detail else ""))
    return ok


def skip(name, detail=""):
    _results.append((SKIP, name, detail))
    print("  %-4s %s  %s" % (SKIP, name, detail))


def summary():
    failed = [r for r in _results if r[0] == FAIL]
    print("")
    print("%d checks, %d failed, %d skipped"
          % (len(_results), len(failed), len([r for r in _results if r[0] == SKIP])))
    return 1 if failed else 0


def load_production_registry(args):
    return sa_config.load_registry(
        args.registry or sa_config.default_registry_path(),
        managed_root=args.managed_root)


def build(args):
    registry = load_production_registry(args)
    os.makedirs(registry.state_dir, exist_ok=True)
    audit = sa_audit.AuditLog(registry.audit_path)
    return registry, sa_broker.SecureBroker(registry, audit=audit,
                                            pin_callback=console_pin)


def _getpass(prompt):
    import getpass
    try:
        return getpass.getpass(prompt)
    except EOFError:
        return None


def console_pin(rp_id=None):
    """FIDO2 PIN prompt for the CTAP paths. Echoes nothing, stores nothing.

    Called by the provider as ``pin_callback(rp_id)``. The value goes
    straight to python-fido2 (through the elevated helper's pipe). It is
    never written to the evidence record, the audit log, a file, a command
    line or the environment, and this function keeps no reference to it.
    """
    try:
        return _getpass("  FIDO2 PIN for %s (not echoed): "
                        % (rp_id or "security key")) or None
    except KeyboardInterrupt:
        return None


def evidence_path(args, registry):
    if getattr(args, "evidence", None):
        return args.evidence
    return ntpath.join(registry.state_dir,
                       "hardware-evidence-%s.json" % time.strftime("%Y%m%d-%H%M%S"))


# ---------------------------------------------------------------- probe
def cmd_probe(args):
    print("== FIDO2 / storage preflight (nothing is modified) ==")
    evidence("python", sys.version.split()[0])
    try:
        import fido2                                  # noqa: F401
        report("fido2 python package present", True)
    except ImportError:
        report("fido2 python package present", False,
               "pip install 'fido2>=2.0,<3'")
    webauthn_available, api_version = fido2_facts()
    installed = _evidence.get("python_fido2_version")
    supported = True
    detail = str(installed)
    try:
        sa_auth.require_supported_fido2(installed or "0")
    except sa_auth.AuthUnavailableError as exc:
        supported = False
        detail = str(exc)
    report("python-fido2 version is supported", supported, detail)
    report("Windows WebAuthn available", webauthn_available,
           "API version %d%s" % (api_version,
                                 "" if api_version >= sa_auth.WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION
                                 else " - too old to carry an hmac-secret salt "
                                      "(version %d is the first that can)"
                                      % sa_auth.WINDOWS_WEBAUTHN_HMAC_SECRET_API_VERSION))
    environment = sa_acceptance.probe_environment()
    evidence("windows_build", environment.get("windows_build"))
    evidence("uac_enabled", environment.get("uac_enabled"))
    evidence("broker_elevated", environment.get("broker_elevated"))
    evidence("acceptance_producer_fingerprint",
             environment.get("acceptance_producer_fingerprint"))
    medium = environment.get("broker_elevated") is False
    evidence("broker_medium_integrity", medium)
    report("this process runs at medium integrity", medium,
           "not elevated" if medium
           else "%s - the disposable acceptance and migrate both refuse to run "
                "from here; %s"
                % (sa_acceptance.BROKER_MUST_RUN_MEDIUM_INTEGRITY,
                   "UAC is disabled (EnableLUA=0), so re-enable it and reboot, or "
                   "use a standard user account"
                   if environment.get("uac_enabled") is False
                   else "start them from an ordinary shell and let Windows prompt "
                        "for the helper's elevation"))
    report("the acceptance producer is measurable",
           environment.get("acceptance_producer_fingerprint") is not None,
           "; ".join(sa_acceptance.ACCEPTANCE_PRODUCER_SOURCES))
    if environment.get("uac_enabled") is False:
        print("  NOTE UAC is disabled on this host (EnableLUA=0): every process of "
              "an administrator is elevated, so on this host the acceptance run "
              "and the migration itself must be started by a standard user -- an "
              "elevated broker is refused, not merely recorded.")
    expected_transport = environment["transport"]
    registry, broker = build(args)
    for profile in registry.profiles:
        print("-- profile %s" % profile.id)
        if profile.id == args.profile:
            evidence("profile_id", profile.id)
            evidence("provider", profile.provider)
            evidence("user_verification_policy", profile.user_verification)
        try:
            caps = sa_enroll.capabilities(broker, profile.id)
        except Exception as exc:
            report("%s: provider reachable" % profile.id, False,
                   "%s: %s" % (type(exc).__name__, exc))
            continue
        # A helper answers capabilities with nothing plugged in, so "a
        # transport exists" is not "an authenticator is connected".
        detected = int(caps.get("authenticators") or 0) >= 1
        if profile.id == args.profile:
            evidence("transport", caps.get("transport"))
            evidence("authenticator_detected", detected)
            evidence("hmac_secret_available", bool(caps["hmac_secret"]))
            evidence("user_verification_available",
                     bool(caps.get("user_verification")))
        report("%s: transport" % profile.id,
               caps.get("transport") == expected_transport,
               "%s (expected %s) - %s" % (caps.get("transport"), expected_transport,
                                          caps.get("detail", "")))
        report("%s: authenticator detected" % profile.id, detected,
               "%s authenticator(s) - %s" % (caps.get("authenticators"),
                                             caps.get("detail", "")))
        report("%s: hmac-secret supported" % profile.id, bool(caps["hmac_secret"]),
               "" if caps["hmac_secret"] else
               "encrypted-vault enrollment will be REFUSED (by design)")
        report("%s: user verification available" % profile.id,
               bool(caps.get("user_verification")),
               "profile policy: %s" % profile.user_verification)
        is_enrolled = broker.credentials.is_enrolled(profile.id)
        report("%s: enrolled" % profile.id,
               True,
               ("%d key(s)" % len(sa_enroll.enrolled_keys(broker, profile.id)))
               if is_enrolled else "0 key(s) (not yet enrolled)")
        if profile.id == args.profile:
            evidence("enrolled_keys",
                     len(sa_enroll.enrolled_keys(broker, profile.id)))
            gate = sa_acceptance.evaluate(registry.state_dir, profile, environment)
            evidence("migration_gate", "accepted" if gate.ok else "blocked")
            print("  gate  migration hardware acceptance: %s"
                  % ("accepted" if gate.ok else "blocked"))
            for reason in gate.reasons:
                print("        - %s" % reason)
        try:
            state = broker.backend(profile).state(profile)
            report("%s: storage reachable" % profile.id, True, "state=%s" % state)
        except Exception as exc:
            report("%s: storage reachable" % profile.id, False,
                   "%s (elevation may have been declined)" % type(exc).__name__)
    broker.shutdown()
    write_evidence(evidence_path(args, registry))
    return summary()


# ---------------------------------------------------------------- enroll
def acknowledge(recovery, profile):
    print("")
    print("=" * 72)
    print(" BitLocker RECOVERY MATERIAL for profile '%s'" % profile.id)
    print(" Shown ONCE. SAITULS stores it nowhere.")
    print("-" * 72)
    print(" %s" % recovery)
    print("=" * 72)
    return input("Type  I HAVE STORED IT  to continue: ").strip().upper() == \
        "I HAVE STORED IT"


def cmd_enroll(args):
    print("== INTERACTIVE enrollment: touch your security key when it blinks ==")
    registry, broker = build(args)
    try:
        if args.additional:
            result = sa_enroll.enroll_additional(broker, args.profile)
        else:
            result = sa_enroll.create_and_enroll(broker, args.profile, acknowledge,
                                                 size_gb=args.size_gb)
        report("enrollment completed", result.ok, result.reason)
        verdict("enrollment", result.ok)
        print(json.dumps(result.to_dict(), indent=2))
    except Exception as exc:
        report("enrollment completed", False, "%s: %s" % (type(exc).__name__, exc))
        verdict("enrollment", False)
    finally:
        broker.shutdown()
    evidence("profile_id", args.profile)
    write_evidence(evidence_path(args, registry))
    return summary()


# ---------------------------------------------------------------- unlock
def cmd_unlock(args):
    print("== INTERACTIVE unlock: touch your security key when it blinks ==")
    registry, broker = build(args)
    try:
        broker.reconcile(args.profile)
        before = broker.status(args.profile)
        report("profile starts locked", before["state"] in
               (sa_state.LOCKED, sa_state.AUTH_REQUIRED, sa_state.SESSION_CACHED),
               before["state"])
        started = time.time()
        result = broker.open(args.profile, force_auth=True)
        report("unlock + mount + launch", result.ok, result.reason)
        report("a key interaction was required", result.required_auth)
        profile = registry.get(args.profile)
        report("vault root exists while unlocked", os.path.isdir(profile.mount_path),
               profile.mount_path)
        print("  took %.1fs" % (time.time() - started))
        input("  Look at the running application, then press Enter to lock...")
        results = broker.lock_now(args.profile)
        report("secure relock", all(r.ok for r in results),
               "; ".join(r.reason for r in results))
        detached = (not os.path.isdir(profile.mount_path)
                    or not os.listdir(profile.mount_path))
        report("vault unreadable after lock", detached, profile.mount_path)
        verdict("unlock", result.ok)
        verdict("vault_detached", detached)
    finally:
        broker.shutdown()
    evidence("profile_id", args.profile)
    write_evidence(evidence_path(args, registry))
    return summary()


# ---------------------------------------------------------------- lifecycle
def cmd_lifecycle(args):
    """The acceptance list, with a human confirming what a test cannot."""
    print("== INTERACTIVE lifecycle acceptance ==")
    registry, broker = build(args)
    profile = registry.get(args.profile)
    try:
        broker.reconcile(args.profile)
        first = broker.open(args.profile, force_auth=True)
        report("1. locked vault requires the authenticator", first.required_auth,
               first.reason)
        report("2. vault readable only while open", os.path.isdir(profile.mount_path))
        input("  Close the protected application, then press Enter...")
        deadline = time.time() + 60
        while time.time() < deadline and broker.status(args.profile)["app_running"]:
            broker.tick()
            time.sleep(1)
        broker.tick()
        status = broker.status(args.profile)
        report("3a. vault detached when the application closed",
               broker.backend(profile).state(profile) != sa_storage.MOUNTED,
               status["state"])
        evidence("mode", status["mode"])
        if status["mode"] == "default":
            report("3b. authentication session still cached",
                   status["session_active"],
                   "expires in %ss" % status["session_expires_in_seconds"])
            second = broker.open(args.profile)
            reopened = bool(second.ok and not second.required_auth)
            report("3c. reopen needed no second key touch", reopened,
                   second.reason)
            verdict("default_reopen", reopened)
            input("  Close the application again, then press Enter...")
            while broker.status(args.profile)["app_running"]:
                broker.tick()
                time.sleep(1)
            broker.tick()
        else:
            report("3b. aggressive mode invalidated the session",
                   not status["session_active"])
            third = broker.open(args.profile)
            report("3c. aggressive reopen demanded the key again",
                   third.required_auth, third.reason)
            verdict("aggressive_reopen_requires_auth", bool(third.required_auth))
            input("  Close the application again, then press Enter...")
            while broker.status(args.profile)["app_running"]:
                broker.tick()
                time.sleep(1)
            broker.tick()
        print("  Lock your workstation (Win+L), unlock it, then press Enter...")
        input()
        broker.on_system_event(sa_broker.EVENT_WORKSTATION_LOCK)
        status = broker.status(args.profile)
        relocked = broker.backend(profile).state(profile) != sa_storage.MOUNTED
        report("6. workstation lock forced a relock", relocked, status["state"])
        verdict("workstation_lock_relock", relocked)
        detached = (not os.path.isdir(profile.mount_path)
                    or not os.listdir(profile.mount_path))
        report("6b. vault detached after the relock", detached,
               profile.mount_path)
        verdict("vault_detached", detached)
        records, allow, secret = scan_audit(registry.audit_path)
        clean = bool(records) and not allow and not secret
        report("7. audit log carries no secret", clean, registry.audit_path)
        verdict("audit_log_clean", clean)
    finally:
        broker.shutdown()
    evidence("profile_id", args.profile)
    write_evidence(evidence_path(args, registry))
    return summary()


# ═══════════════════════════════════════════ disposable hardware acceptance
#: Every property the disposable run proves, and the gate flag it feeds.
#: A gate flag is True only when EVERY check mapped to it has passed and
#: none of them failed or was skipped.
CHECKS = (
    ("F01", "fido2_hardware_accepted", "transport is the one this host must use"),
    ("F02", "fido2_hardware_accepted", "authenticator detected"),
    ("F03", "fido2_hardware_accepted", "CTAP2 hmac-secret supported"),
    ("F04", "fido2_hardware_accepted", "clientPin / user verification available"),
    ("F05", "fido2_hardware_accepted", "credential creation succeeds"),
    ("F06", "fido2_hardware_accepted", "correct PIN + touch authenticates, nothing mounted"),
    ("F07", "fido2_hardware_accepted", "hmac-secret unwrap reproduces the volume secret"),
    ("F08", "fido2_hardware_accepted", "cancelled PIN fails closed"),
    ("F09", "fido2_hardware_accepted", "wrong PIN fails closed"),
    ("F10", "fido2_hardware_accepted", "no key fails closed"),
    ("F11", "fido2_hardware_accepted", "wrong enrolled credential fails closed"),
    ("P01", "silent_privileged_start_accepted",
     "privileged helper task installed with the expected fixed definition"),
    ("P02", "silent_privileged_start_accepted",
     "Task Scheduler starts the helper with NO Windows consent prompt"),
    ("P03", "silent_privileged_start_accepted",
     "helper arrives at high integrity while this broker stays medium"),
    ("P04", "silent_privileged_start_accepted",
     "a changed task definition is refused as PRIVILEGED_TASK_TAMPERED"),
    ("P05", "privileged_runtime_acl_accepted",
     "Windows REALLY denies this medium process every write to the protected "
     "runtime, the worker bundle, the manifest and the pin"),
    ("P06", "privileged_task_security_accepted",
     "Windows REALLY lets this medium process query and run the task, and "
     "REALLY denies changing, disabling, deleting and re-securing it"),
    ("P07", "privileged_runtime_acl_accepted",
     "the pin is owned by Administrators/SYSTEM and the recursive FIDO worker "
     "bundle fingerprint matches the one pinned at installation"),
    ("S01", "storage_accepted", "BitLocker container with recovery protector, left detached"),
    ("S02", "storage_accepted", "encrypted volume write and read"),
    ("S03", "storage_accepted", "volume detached when the application exits"),
    ("S04", "storage_accepted", "broker shutdown closes the application and detaches storage"),
    ("D01", "default_mode_accepted", "Default reopen uses the cached volume secret, no FIDO interaction"),
    ("A01", "aggressive_mode_accepted", "Aggressive open requires authentication"),
    ("A02", "aggressive_mode_accepted", "Aggressive reopen requires authentication again"),
    ("W01", "workstation_lock_accepted", "real workstation lock notification received"),
    ("W02", "workstation_lock_accepted", "workstation lock destroys the session and detaches storage"),
    ("H01", "helper_failure_accepted", "elevated helper terminated while the vault is mounted"),
    ("H02", "helper_failure_accepted", "a dead helper never reads as LOCKED"),
    ("H03", "helper_failure_accepted", "recovery after the helper's death really relocks"),
    ("X01", "audit_hygiene_accepted", "audit records are allowlisted"),
    ("X02", "audit_hygiene_accepted", "audit output carries no secret"),
)
CHECK_FLAGS = {check_id: flag for check_id, flag, _text in CHECKS}
CHECK_TEXT = {check_id: text for check_id, _flag, text in CHECKS}

DISPOSABLE_PROFILE = "disposable-acceptance"
WRONG_PIN = "saituls-acceptance-deliberately-wrong-pin"
LOCK_WAIT_SECONDS = 300
MIN_PIN_SUBSTRING = 6

#: The disposable "protected application": runs until the run drops a stop
#: file, so every exit is the one the sequence asked for.
APP_SCRIPT = ("import os, sys, time\n"
              "stop = sys.argv[1]\n"
              "while not os.path.exists(stop):\n"
              "    time.sleep(0.2)\n")



from sa_acceptance_runner import (
    AbortAcceptance,
    AcceptanceRun,
    PinPrompt,
    WorkstationLockListener,
    scan_audit,
    disposable_registry_document,
    DisposableAcceptance,
    cmd_disposable_acceptance,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="SAITULS Secure Apps interactive hardware acceptance "
                    "(physical FIDO2 key required; never run in CI)")
    parser.add_argument("--registry", default=None)
    parser.add_argument("--managed-root", default=None)
    parser.add_argument("--profile", default="obsidian",
                        help="production profile whose policy the run mirrors")
    parser.add_argument("--size-gb", type=int, default=None)
    parser.add_argument("--additional", action="store_true")
    parser.add_argument("--mount-parent", default=None,
                        help="directory for the disposable mount point (default: "
                             "the parent of the profile's real mount path, so the "
                             "run exercises the same volume)")
    parser.add_argument("--evidence", default=None,
                        help="where to write the non-secret evidence record")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--probe", action="store_true")
    group.add_argument("--enroll", action="store_true")
    group.add_argument("--unlock", action="store_true")
    group.add_argument("--lifecycle", action="store_true")
    group.add_argument("--disposable-acceptance", action="store_true",
                       help="prove every migration prerequisite against a "
                            "throwaway container and record the result")
    args = parser.parse_args(argv)
    if args.probe:
        return cmd_probe(args)
    if args.enroll:
        return cmd_enroll(args)
    if args.unlock:
        return cmd_unlock(args)
    if args.lifecycle:
        return cmd_lifecycle(args)
    return cmd_disposable_acceptance(args)


if __name__ == "__main__":
    sys.exit(main())
