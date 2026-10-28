"""Command-line surface for SAITULS Secure Apps.

The GUI is the normal way in. This exists for three things the GUI cannot be:
a non-interactive probe that CI can run (``selftest``), a headless supervised
session for a machine with no desktop (``open``), and the migration driver,
which is a long operation better watched in a console than in a modal.

Nothing here ever prints, echoes or accepts a secret on a command line. The
one secret a human ever sees is the BitLocker recovery material during first
enrollment, shown once, on a terminal, and never written anywhere by SAITULS.
"""
import argparse
import json
import ntpath
import os
import sys
import time

HERE = ntpath.dirname(ntpath.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import sa_acceptance      # noqa: E402
import sa_applife          # noqa: E402
import sa_audit           # noqa: E402
import sa_auth            # noqa: E402
import sa_backup          # noqa: E402
import sa_broker          # noqa: E402
import sa_config          # noqa: E402
import sa_enroll          # noqa: E402
import sa_ffs             # noqa: E402
import sa_migrate         # noqa: E402
import sa_mnemonic        # noqa: E402
import sa_capacity        # noqa: E402
import sa_paths           # noqa: E402
import sa_privtask        # noqa: E402
import sa_recovery        # noqa: E402
import sa_state           # noqa: E402
import sa_storage         # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFIG = 3
EXIT_REFUSED = 4
EXIT_FAILED = 5


def emit(payload, as_json):
    if as_json:
        print(json.dumps(payload, indent=None, sort_keys=False, default=str))
    else:
        if isinstance(payload, dict):
            for key, value in payload.items():
                print("%-26s %s" % (key, value))
        elif isinstance(payload, list):
            for item in payload:
                print(json.dumps(item, default=str))
        else:
            print(payload)


def load(args):
    registry = sa_config.load_registry(
        args.registry or sa_config.default_registry_path(),
        managed_root=args.managed_root)
    return registry


def _console_pin(rp_id):
    """Ask for the FIDO2 PIN without echoing it, and keep it nowhere.

    Used only on the direct CTAP path. The value is returned straight to
    python-fido2; it is never stored, logged, written to a file, put on a
    command line or exported into the environment.
    """
    import getpass
    try:
        return getpass.getpass("FIDO2 PIN for %s (not echoed): " % rp_id) or None
    except (EOFError, KeyboardInterrupt):
        return None


def build_broker(registry, echo=False, pin_callback=_console_pin):
    os.makedirs(registry.state_dir, exist_ok=True)
    audit = sa_audit.AuditLog(registry.audit_path,
                              echo=(lambda line: print(line)) if echo else None)
    return sa_broker.SecureBroker(registry, audit=audit,
                                  pin_callback=pin_callback)


# --------------------------------------------------------------------------
def cmd_profiles(args):
    registry = load(args)
    emit([p.to_dict() for p in registry.profiles], args.json)
    return EXIT_OK


def cmd_status(args):
    registry = load(args)
    broker = build_broker(registry)
    rows = []
    for profile in registry.profiles:
        broker.reconcile(profile.id)
        rows.append(broker.status(profile.id))
    emit(rows if args.json else rows, args.json)
    return EXIT_OK


def cmd_selftest(args):
    """Non-interactive probe. No authenticator, no elevation, no mutation."""
    result = {
        "ok": True,
        "registry": None,
        "schema_version": None,
        "managed_root": None,
        "profiles": [],
        "problems": [],
        "python": sys.version.split()[0],
        "modules": {},
    }
    for name in ("cryptography", "psutil", "win32pipe", "fido2", "PyQt6"):
        try:
            module = __import__(name)
            result["modules"][name] = "present"
        except Exception:
            result["modules"][name] = "absent"
            module = None
        if name == "fido2" and module is not None:
            try:
                from importlib import metadata
                version = metadata.version("fido2")
                result["modules"]["fido2"] = "present " + version
                sa_auth.require_supported_fido2(version)
            except sa_auth.AuthUnavailableError as exc:
                result["ok"] = False
                result["problems"].append(str(exc))
            except Exception:
                pass
    if result["modules"]["cryptography"] == "absent":
        result["ok"] = False
        result["problems"].append("cryptography is required for the key hierarchy")
    try:
        registry = load(args)
    except sa_config.ConfigError as exc:
        result["ok"] = False
        result["problems"].append("registry: %s" % exc)
        emit(result, True)
        return EXIT_CONFIG
    result["registry"] = args.registry or sa_config.default_registry_path()
    result["schema_version"] = registry.schema_version
    result["managed_root"] = registry.managed_root
    for profile in registry.profiles:
        entry = {
            "id": profile.id,
            "label": profile.label,
            "enabled": profile.enabled,
            "backend": profile.backend,
            "provider": profile.provider,
            "mode": profile.policy.mode,
            "idle_timeout_minutes": profile.policy.idle_timeout_minutes,
            "conditions": [c.type for c in profile.policy.conditions],
            "container": profile.container,
            "mount_path": profile.mount_path,
            "user_verification": profile.user_verification,
            "executable": profile.executable,
            "executable_present": os.path.isfile(profile.executable),
            "container_present": os.path.isfile(profile.container),
            "mount_path_present": os.path.isdir(profile.mount_path),
        }
        try:
            store = sa_auth.CredentialStore(registry.credentials_path)
            entry["enrolled"] = store.is_enrolled(profile.id)
            entry["enrolled_keys"] = len(store.enrollments(profile.id))
        except sa_auth.AuthError as exc:
            entry["enrolled"] = None
            result["problems"].append("credentials: %s" % exc)
            result["ok"] = False
        result["profiles"].append(entry)
    emit(result, True)
    return EXIT_OK if result["ok"] else EXIT_FAILED


def cmd_capabilities(args):
    registry = load(args)
    broker = build_broker(registry)
    try:
        caps = sa_enroll.capabilities(broker, args.profile)
    except Exception as exc:
        emit({"ok": False, "error": str(exc)}, True)
        return EXIT_FAILED
    caps["ok"] = True
    emit(caps, True)
    return EXIT_OK if caps.get("hmac_secret") else EXIT_REFUSED


def _acknowledge_on_console(recovery, profile):
    print("")
    print("=" * 72)
    print(" BitLocker RECOVERY MATERIAL for profile '%s'" % profile.id)
    print(" This is shown ONCE. SAITULS does not store it anywhere.")
    print(" Without it, losing every enrolled security key means losing the")
    print(" vault. Write it down or store it in a password manager NOW.")
    print("-" * 72)
    print(" %s" % recovery)
    print("=" * 72)
    print("")
    answer = input("Type  I HAVE STORED IT  to continue (anything else aborts): ")
    return answer.strip().upper() == "I HAVE STORED IT"


def cmd_enroll(args):
    registry = load(args)
    broker = build_broker(registry)
    try:
        if args.additional:
            result = sa_enroll.enroll_additional(broker, args.profile)
        else:
            portable_pw = None
            if getattr(args, "generate_password", False):
                portable_pw = sa_mnemonic.generate_portable_passphrase(9)
                print("\n" + "=" * 72)
                print("GENERATED PORTABLE MASTER PASSWORD (9 words):")
                print("  " + portable_pw)
                print("=" * 72 + "\n")
            elif sys.stdin.isatty():
                import getpass
                entered = getpass.getpass("Enter Portable Master Password (or press ENTER to generate 9-word phrase): ").strip()
                if not entered:
                    portable_pw = sa_mnemonic.generate_portable_passphrase(9)
                    print("\n" + "=" * 72)
                    print("GENERATED PORTABLE MASTER PASSWORD (9 words):")
                    print("  " + portable_pw)
                    print("=" * 72 + "\n")
                else:
                    portable_pw = entered
                    ok, msg = sa_mnemonic.validate_master_password(portable_pw)
                    if not ok:
                        emit({"ok": False, "error_category": "config_invalid", "message": msg}, True)
                        return EXIT_REFUSED

            result = sa_enroll.create_and_enroll(broker, args.profile,
                                                 _acknowledge_on_console,
                                                 size_gb=args.size_gb,
                                                 portable_password=portable_pw)
    except (sa_enroll.EnrollmentRefused, sa_auth.AuthError) as exc:
        emit({"ok": False, "error_category": getattr(exc, "category", "auth_failed"),
              "message": str(exc)}, True)
        return EXIT_REFUSED
    finally:
        broker.shutdown()
    emit(result.to_dict(), True)
    return EXIT_OK if result.ok else EXIT_REFUSED


def cmd_credential(args):
    action = getattr(args, "cred_action", None)
    registry = load(args)
    broker = build_broker(registry)
    try:
        if action == "list":
            keys = sa_enroll.enrolled_keys(broker, args.profile)
            emit({"ok": True, "profile": args.profile, "credentials": keys}, True)
            return EXIT_OK
        elif action == "remove":
            result = sa_enroll.remove_credential_authenticated(
                broker, args.profile, args.credential)
            emit(result.to_dict(), True)
            return EXIT_OK if result.ok else EXIT_REFUSED
        else:
            emit({"ok": False, "message": "unknown credential action %r" % action}, True)
            return EXIT_CONFIG
    except (sa_auth.AuthError, sa_enroll.EnrollmentRefused) as exc:
        emit({"ok": False, "error_category": getattr(exc, "category", "auth_failed"),
              "message": str(exc)}, True)
        return EXIT_REFUSED
    finally:
        broker.shutdown()


def cmd_import_app(args):
    registry = load(args)
    broker = build_broker(registry)
    try:
        result = sa_enroll.import_application(broker, args.profile, args.source,
                                              overwrite=args.overwrite)
    except sa_enroll.EnrollmentRefused as exc:
        emit({"ok": False, "error_category": exc.category, "message": str(exc)}, True)
        return EXIT_REFUSED
    finally:
        broker.shutdown()
    emit(result, True)
    return EXIT_OK


def cmd_open(args):
    registry = load(args)
    guard = sa_broker.SingleInstanceGuard()
    if not guard.acquire():
        emit({"ok": False, "error_category": "concurrent_request",
              "message": "a Secure Apps broker is already running; use its "
                         "window instead of a second one"}, True)
        return EXIT_REFUSED
    broker = build_broker(registry, echo=args.verbose)
    try:
        broker.reconcile(args.profile)
        result = broker.open(args.profile, force_auth=args.force_auth)
        emit(result.to_dict(), True)
        if not result.ok:
            return EXIT_REFUSED
        if args.detach:
            print("NOTE: --detach leaves no broker running. The vault stays "
                  "mounted until a broker relocks it.", file=sys.stderr)
            return EXIT_OK
        # Supervise until the policy says the profile is locked again.
        while True:
            time.sleep(min(5.0, args.poll_seconds))
            broker.poll_foreground()
            for event in broker.tick():
                if event is not None:
                    emit(event.to_dict(), True)
            status = broker.status(args.profile)
            if status["state"] in (sa_state.LOCKED, sa_state.AUTH_REQUIRED,
                                   sa_state.ERROR) and not status["app_running"]:
                emit(status, True)
                return EXIT_OK
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        try:
            broker.shutdown()
        finally:
            guard.release()


def cmd_emergency(args):
    """Scripted emergency recovery (T-254): mount, letter, unlock, lock.

    Mirrors the printed emergency steps exactly, one truthful step at a
    time. Refusals stop the sequence by construction -- there is no code
    path that continues past a non-ok step. The recovery password, when a
    step needs one, is read from stdin with no echo and never echoed,
    logged, or written anywhere.
    """
    container = getattr(args, "container", None)
    if not container:
        registry = load(args)
        try:
            profile = registry.get(args.profile)
        except sa_config.ConfigError:
            emit({"ok": False, "error_category": "config_invalid",
                  "message": "unknown profile id: %s" % args.profile}, True)
            return EXIT_CONFIG
        container = getattr(profile, "container", None)
    if not container or not os.path.isfile(container):
        emit({"ok": False, "error_category": "config_invalid",
              "message": "container not found: %s" % (container or "<none specified>")},
             True)
        return EXIT_CONFIG

    if args.step == "attach":
        status, data, detail = sa_recovery.attach_with_letter(container)
    elif args.step in ("unlock", "all"):
        import getpass
        password = getpass.getpass("BitLocker recovery password (input hidden): ")
        status, data, detail = sa_recovery.unlock_with_password(
            container, args.letter, password)
    else:  # "close"
        status, data, detail = sa_recovery.lock_and_dismount(container)

    emit({"step": args.step, "status": status,
          "result": data, "detail": detail}, True)
    if status == sa_recovery.RETURN_OK:
        return EXIT_OK
    if status in (sa_recovery.RETURN_REFUSED, sa_recovery.RETURN_MISSING):
        return EXIT_REFUSED
    return EXIT_FAILED


def cmd_capacity(args):
    """T-256: measured capacity truth for one profile's container.

    Mounts nothing, needs no elevation, never guesses: missing container and
    unmeasurable host free space are reported as their truth, not zeros.
    """
    registry = load(args)
    try:
        profile = registry.get(args.profile)
    except sa_config.ConfigError:
        emit({"ok": False, "error_category": "config_invalid",
              "message": "unknown profile id: %s" % args.profile}, True)
        return EXIT_CONFIG
    facts = sa_capacity.container_facts(
        getattr(profile, "container", None) or "",
        getattr(profile, "size_gb", None))
    facts["verdicts"] = sa_capacity.classify(facts)
    plan = refusal = None
    # "omitted" and "explicitly zero" are different requests. A read-only
    # capacity probe is what omission asks for; --grow-gb 0 is an operator
    # naming an impossible size and must be an error, not a silent no-op.
    if args.grow_gb is not None:
        plan, refusal = sa_capacity.plan_grow(
            facts["container"], facts["provisioned_gb"], args.grow_gb)
    emit({"ok": refusal is None,
          "capacity": facts,
          "plan": plan,
          "refusal": refusal}, True)
    return EXIT_OK if refusal is None else EXIT_FAILED


def cmd_lock(args):
    registry = load(args)
    broker = build_broker(registry)
    try:
        for profile in registry.profiles:
            broker.reconcile(profile.id)
        results = broker.lock_now(args.profile)
        emit([r.to_dict() for r in results], True)
        return EXIT_OK if all(r.ok for r in results) else EXIT_FAILED
    finally:
        broker.shutdown()


def cmd_mode(args):
    registry = load(args)
    broker = build_broker(registry)
    try:
        result = broker.set_mode(args.profile, args.mode)
        emit(result.to_dict(), True)
        return EXIT_OK
    finally:
        broker.shutdown()


def cmd_recover(args):
    registry = load(args)
    broker = build_broker(registry)
    try:
        broker.reconcile(args.profile)
        result = broker.recover(args.profile)
        emit(result.to_dict(), True)
        return EXIT_OK if result.ok else EXIT_FAILED
    finally:
        broker.shutdown()


def _report_acceptance_blocked(args, registry, profile, verdict):
    """Plain token first, so neither a human nor a script can miss it."""
    command = sa_acceptance.acceptance_command(
        profile_id=profile.id, registry_path=args.registry,
        managed_root=args.managed_root)
    print(sa_acceptance.MIGRATION_BLOCKED)
    for reason in verdict.reasons:
        print("  reason: %s" % reason)
    print("  Run the disposable hardware acceptance first (security key, PIN, "
          "touch, workstation lock); it never touches a real vault:")
    print("  %s" % command)
    if not os.path.isfile(sa_acceptance.ACCEPTANCE_SCRIPT):
        print("  NOTE: %s is not part of this install; run it from a full "
              "SAITULS checkout" % sa_acceptance.ACCEPTANCE_SCRIPT)
    sa_audit.AuditLog(registry.audit_path).write(
        "migrate_gate", profile_id=profile.id, result="refused",
        error_category="hardware_acceptance_required",
        auth_provider=profile.provider, backend=profile.backend)
    payload = verdict.to_dict()
    payload["acceptance_command"] = command
    emit(payload, True)


def _report_acceptance_recognized(verdict):
    print("HARDWARE_ACCEPTANCE_RECOGNIZED")
    for token in sa_acceptance.FINAL_TOKENS:
        print(token)
    emit(verdict.to_dict(), True)


def cmd_migrate(args):
    if getattr(args, "check_readiness", False):
        registry = load(args)
        profile = registry.get(args.profile)
        if not args.source:
            emit({"ok": False, "error_category": "config_invalid",
                  "message": "--check-readiness requires --source <path>"}, True)
            return EXIT_USAGE
        readiness = sa_migrate.evaluate_recovery_readiness(registry.state_dir, profile, args.source)
        emit(readiness.to_dict(), True)
        return EXIT_OK if readiness.ok else EXIT_REFUSED

    if getattr(args, "drill_readiness", False):
        registry = load(args)
        profile = registry.get(args.profile)
        if not args.source:
            emit({"ok": False, "error_category": "config_invalid",
                  "message": "--drill-readiness requires --source <path>"}, True)
            return EXIT_USAGE
        backup_dest = getattr(args, "backup_dest", None) or sa_backup.DEFAULT_BACKUP_DIR
        try:
            record = sa_migrate.create_recovery_readiness_proof(
                registry.state_dir, profile, args.source, backup_dest
            )
            emit({"ok": True, "verdict": "RECOVERY_READY", "record": record}, True)
            return EXIT_OK
        except Exception as exc:
            emit({"ok": False, "error_category": "drill_failed", "message": str(exc)}, True)
            return EXIT_FAILED

    if not args.check_acceptance and not args.source:
        emit({"ok": False, "error_category": "config_invalid",
              "message": "migrate needs --source <plaintext vault> "
                         "(or --check-acceptance to only test the gate)"}, True)
        return EXIT_USAGE
    registry = load(args)
    profile = registry.get(args.profile)
    # The hardware-acceptance gate runs first: before the single-instance
    # mutex, before a broker, before the elevated helper exists at all. A
    # refused migration asks for no key and raises no elevation.
    verdict = sa_acceptance.evaluate(registry.state_dir, profile)
    if not verdict.ok:
        _report_acceptance_blocked(args, registry, profile, verdict)
        return EXIT_REFUSED
    if args.check_acceptance:
        _report_acceptance_recognized(verdict)
        return EXIT_OK

    # Recovery readiness gate runs next, before any broker lock or authentication:
    readiness = sa_migrate.evaluate_recovery_readiness(registry.state_dir, profile, args.source)
    if not readiness.ok:
        emit({"ok": False, "error_category": "recovery_readiness_required",
              "message": "migration blocked: %s" % "; ".join(readiness.reasons),
              "readiness": readiness.to_dict()}, True)
        return EXIT_REFUSED

    guard = sa_broker.SingleInstanceGuard()
    if not guard.acquire():
        emit({"ok": False, "message": "a Secure Apps broker is already running"}, True)
        return EXIT_REFUSED
    broker = build_broker(registry, echo=True)
    try:
        report = sa_migrate.migrate(
            broker, args.profile, args.source,
            verify_application=not args.no_app_verify,
            restart=args.restart)
        emit(report.to_dict(), True)
        return EXIT_OK if report.ok else EXIT_FAILED
    finally:
        try:
            broker.shutdown()
        finally:
            guard.release()


def cmd_migrate_status(args):
    registry = load(args)
    path = ntpath.join(registry.state_dir, "migration-%s.json" % args.profile)
    emit(sa_migrate.inspect(path), True)
    return EXIT_OK


def cmd_migrate_recover(args):
    registry = load(args)
    broker = build_broker(registry)
    try:
        result = sa_migrate.recover(broker, args.profile)
        emit(result, True)
        if result.get("action") == "refused":
            return EXIT_REFUSED
        return EXIT_OK
    finally:
        broker.shutdown()


# --------------------------------------------------------------------------
# the pre-authorized privilege boundary
# --------------------------------------------------------------------------
def _write_result(args, payload):
    """Hand a result back to the medium-integrity parent that elevated us.

    An elevated child's stdout does not reach the process that started it
    across the elevation boundary, so the answer travels through a file the
    parent named. It carries public facts only -- paths, booleans, hashes.
    """
    path = getattr(args, "result", None)
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, default=str)
    except OSError:
        pass


def _elevated_step(arguments):
    """Run one setup step elevated and read what it reported.

    This is the ONE consent dialog Secure Apps ever raises, and only Install,
    Repair, Remove and Prepare reach it. Open and Lock never do.
    """
    import tempfile
    handle, result_path = tempfile.mkstemp(prefix="saituls-setup-", suffix=".json")
    os.close(handle)
    try:
        completed = sa_privtask.elevate_self(arguments, result_path=result_path)
        payload = {}
        try:
            with open(result_path, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, ValueError):
            payload = {}
        if not payload:
            payload = {"ok": False, "message":
                       "the elevated setup step reported nothing (exit %s); the "
                       "consent dialog may have been declined"
                       % getattr(completed, "returncode", "?")}
        return payload
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass


def run_privileged_setup(action, idle_timeout=None, script_dir=None,
                         confirm=False):
    """Install / repair / remove / prepare, from a medium-integrity caller.

    The same function behind the CLI subcommand and the GUI dialog, so the
    refusals a person sees are the same ones either way. The elevated child
    does the registration; THIS process decides whether what came back is
    really a silent privilege boundary (:func:`sa_privtask.commission`).
    """
    script_dir = script_dir or ntpath.dirname(ntpath.abspath(__file__))
    if action == "preflight":
        return sa_privtask.preflight(script_dir=script_dir)
    if action == "check":
        return sa_privtask.verify_installation(script_dir=script_dir).to_dict()
    if action == "prepare-uac":
        if not confirm:
            return {"ok": False, "error_category": "config_invalid",
                    "action": action,
                    "message": "Prepare Windows privilege separation sets "
                               "EnableLUA=1 and nothing else, and Windows must "
                               "be restarted afterwards. Your normal UAC prompt "
                               "policy is not changed. Confirm to proceed.",
                    "uac": sa_privtask.uac_policy_facts()}
        payload = _elevated_step(["privileged-helper", "prepare-uac",
                                  "--confirm", "--apply"])
        payload["action"] = action
        return payload
    if action == "remove":
        payload = _elevated_step(["privileged-helper", "remove", "--apply"])
        payload["action"] = action
        return payload
    if action not in ("install", "repair"):
        return {"ok": False, "error_category": "config_invalid", "action": action,
                "message": "unknown privileged-helper action %r" % (action,)}

    report = sa_privtask.preflight(script_dir=script_dir)
    if report.get("EnableLUA") is False:
        return {"ok": False, "error_category": "config_invalid", "action": action,
                "message": "EnableLUA is 0 on this host: Windows privilege "
                           "separation is off, so there is no medium-integrity "
                           "broker to protect and no boundary for a scheduled "
                           "helper to cross. Run 'Prepare Windows privilege "
                           "separation' first, reboot, then install.",
                "preflight": report}
    if report.get("user_is_administrator") is False:
        return {"ok": False, "error_category": "config_invalid", "action": action,
                "message": "this account cannot register the privileged helper "
                           "task; an administrator must run the setup",
                "preflight": report}
    arguments = ["privileged-helper", action, "--apply"]
    if idle_timeout:
        arguments += ["--idle-timeout", str(int(idle_timeout))]
    payload = _elevated_step(arguments)
    payload["action"] = action
    # Steps 5-9: the elevated child registered it, this medium process proves
    # it is really a silent privilege boundary -- and stops the helper if not.
    payload["commission"] = sa_privtask.commission(script_dir=script_dir)
    payload["ok"] = bool(payload.get("ok") and payload["commission"]["ok"])
    if payload.get("ok") is False and payload["commission"].get("ok") is False             and payload.get("task_path"):
        # The elevated transaction completed and rolled nothing back -- it
        # verified its own work -- but THIS medium-integrity process could not
        # prove the result is a silent boundary. The installation is
        # consistent, not mixed; what is missing is the proof. Say so, and say
        # exactly what closes it, rather than leaving an unproven silent
        # elevation mechanism installed without comment. Repair and Remove are
        # the two privileged operations that resolve it, and each costs its own
        # ordinary consent dialog -- this path deliberately does not raise one
        # by itself.
        payload["commissioning_failed"] = True
        payload["message"] = (
            "the privileged helper was installed and verified by the elevated "
            "step, but this medium-integrity process could not prove a silent "
            "privileged start (%s). The mechanism is installed and internally "
            "consistent; it is NOT yet proven. Run 'privileged-helper repair' "
            "to rebuild it, or 'privileged-helper remove' to take it off this "
            "machine. Do not migrate a vault until commissioning passes."
            % (payload["commission"].get("token") or "; ".join(
                payload["commission"].get("reasons") or ["no reason reported"])))
    return payload


def cmd_privileged_helper(args):
    script_dir = ntpath.dirname(ntpath.abspath(__file__))
    action = args.action

    # --apply is the elevated child's own entry point: it does the privileged
    # work and reports through the file its medium-integrity parent named.
    if args.apply:
        try:
            if action in ("install", "repair"):
                payload = sa_privtask.apply_install(
                    script_dir=script_dir,
                    idle_timeout_seconds=args.idle_timeout)
            elif action == "remove":
                payload = sa_privtask.apply_remove()
            elif action == "prepare-uac":
                payload = sa_privtask.apply_prepare_uac(confirm=args.confirm)
            else:
                payload = {"ok": False, "message":
                           "%s takes no privileged step" % action}
        except sa_privtask.PrivilegeTaskError as exc:
            payload = {"ok": False, "message": str(exc),
                       "error_category": exc.category, "token": exc.token}
        _write_result(args, payload)
        emit(payload, True)
        return EXIT_OK if payload.get("ok") else EXIT_FAILED

    payload = run_privileged_setup(action, idle_timeout=args.idle_timeout,
                                   script_dir=script_dir, confirm=args.confirm)
    if action == "check" and args.start and payload.get("ok"):
        payload["commission"] = sa_privtask.commission(script_dir=script_dir)
        payload["ok"] = bool(payload["commission"]["ok"])
    emit(payload, True)
    if action == "preflight":
        return EXIT_OK if payload.get("scheduled_helper_definition_valid") else EXIT_REFUSED
    if payload.get("ok"):
        return EXIT_OK
    return EXIT_REFUSED if payload.get("error_category") == "config_invalid" else EXIT_FAILED


def cmd_audit(args):
    registry = load(args)
    path = registry.audit_path
    if not os.path.isfile(path):
        emit({"ok": True, "records": []}, True)
        return EXIT_OK
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()[-args.count:]
    for line in lines:
        print(line.rstrip("\n"))
    return EXIT_OK


def cmd_portable_password(args):
    action = getattr(args, "pw_action", None)
    if action == "generate":
        phrase = sa_mnemonic.generate_portable_passphrase(word_count=9)
        emit({"ok": True, "generated_passphrase": phrase, "words": len(phrase.split())}, args.json)
        return EXIT_OK

    registry = load(args)
    broker = build_broker(registry)
    try:
        profile_id = args.profile
        if action == "list":
            res = broker.list_protectors(profile_id)
            emit(res, args.json)
            return EXIT_OK

        import getpass
        if action == "add":
            pw = getpass.getpass("Enter Portable Master Password (or phrase): ")
            ok, msg = sa_mnemonic.validate_master_password(pw)
            if not ok:
                emit({"ok": False, "error_category": "config_invalid", "message": msg}, args.json)
                return EXIT_REFUSED
            res = broker.add_portable_password(profile_id, pw)
            emit(res, args.json)
            return EXIT_OK

        elif action == "change":
            old_pw = getpass.getpass("Enter Current Portable Master Password: ") or None
            new_pw = getpass.getpass("Enter New Portable Master Password: ")
            ok, msg = sa_mnemonic.validate_master_password(new_pw)
            if not ok:
                emit({"ok": False, "error_category": "config_invalid", "message": msg}, args.json)
                return EXIT_REFUSED
            res = broker.change_portable_password(profile_id, new_pw, old_password=old_pw)
            emit(res, args.json)
            return EXIT_OK

        elif action == "remove":
            res = broker.remove_portable_password(profile_id)
            emit(res, args.json)
            return EXIT_OK

        else:
            emit({"ok": False, "message": "unknown action %r" % action}, args.json)
            return EXIT_CONFIG
    finally:
        broker.shutdown()


def cmd_backup(args):
    action = getattr(args, "backup_action", "publish")
    registry = load(args)
    broker = build_broker(registry)
    try:
        if action == "publish":
            profile_id = getattr(args, "profile", None)
            allow_integrity_only = getattr(args, "allow_integrity_only", False)
            res = broker.publish_backup(profile_id, destination_dir=args.destination,
                                        allow_integrity_only=allow_integrity_only)
            emit(res, args.json)
            return EXIT_OK if res.get("ok") else EXIT_FAILED

        elif action == "verify":
            res = sa_backup.verify_backup(destination_dir=args.destination)
            emit(res, args.json)
            return EXIT_OK if res.get("ok") else EXIT_FAILED

        elif action == "open":
            import getpass
            vault_path = args.vault_path
            mount_path = args.mount_path
            choice = input("Unlock method: [1] Portable Password, [2] 48-Digit Recovery Key, [3] YubiKey [1]: ").strip()
            pw = None
            rec = None
            use_yubikey = False
            if choice == "2":
                rec = getpass.getpass("48-digit Recovery Key: ").strip()
            elif choice == "3":
                use_yubikey = True
            else:
                pw = getpass.getpass("Portable Master Password: ")

            profile = registry.get(registry.ids()[0])
            backend = broker._backend_for(profile)
            res = sa_backup.open_portable_vault(
                backend=backend,
                vault_path=vault_path,
                mount_path=mount_path,
                password=pw,
                recovery_password=rec,
                use_yubikey=use_yubikey,
                read_only=args.read_only
            )
            emit(res, args.json)
            return EXIT_OK

        elif action == "close":
            vault_path = args.vault_path
            mount_path = args.mount_path
            profile = registry.get(registry.ids()[0])
            backend = broker._backend_for(profile)
            res = sa_backup.close_portable_vault(backend, vault_path, mount_path)
            emit(res, args.json)
            return EXIT_OK

    finally:
        broker.shutdown()


def cmd_ffs(args):
    action = getattr(args, "ffs_action", "audit")
    if action in ("audit", "inventory"):
        cfg = sa_ffs.audit_ffs_configuration(getattr(args, "batch", None), getattr(args, "gui", None))
        leakage = sa_ffs.audit_plaintext_leakage_on_destinations()
        out_path = getattr(args, "output", None)
        rep_res = sa_ffs.generate_leakage_report(output_path=out_path)
        overall_safe = bool(cfg.get("overall_safe", False))
        report = {
            "ok": overall_safe,
            "audit_completed": True,
            "overall_safe": overall_safe,
            "configuration": cfg,
            "destinations_leakage_audit": leakage,
            "report_file": rep_res.get("output_path"),
            "local_detailed_report": rep_res.get("local_detailed_path")
        }
        emit(report, args.json)
        return EXIT_OK if overall_safe else EXIT_USAGE

    elif action == "harden":
        res = sa_ffs.apply_ffs_hardening(getattr(args, "batch", None), getattr(args, "gui", None), force=getattr(args, "force", False))
        emit(res, args.json)
        return EXIT_OK if (res.get("ok") and res.get("overall_safe")) else EXIT_USAGE

    elif action == "restore":
        res = sa_ffs.restore_ffs_backup(getattr(args, "batch", None), getattr(args, "gui", None))
        emit(res, args.json)
        return EXIT_OK


# --------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="secure-apps",
        description="SAITULS Secure Apps - FIDO2-gated protected application launcher")
    parser.add_argument("--registry", default=None,
                        help="path to secure_apps.json")
    parser.add_argument("--managed-root", default=None,
                        help="override the managed sidecar storage root")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("profiles", help="list validated profiles").set_defaults(
        func=cmd_profiles)
    sub.add_parser("status", help="reconcile and report every profile").set_defaults(
        func=cmd_status)
    sub.add_parser("selftest", help="non-interactive configuration probe").set_defaults(
        func=cmd_selftest)

    p = sub.add_parser("capabilities", help="what the connected authenticator can do")
    p.add_argument("profile")
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("enroll", help="create the vault and enroll a key")
    p.add_argument("profile")
    p.add_argument("--additional", action="store_true",
                   help="add another key to an already enrolled vault")
    p.add_argument("--size-gb", type=int, default=None)
    p.add_argument("--generate-password", action="store_true",
                   help="generate and display a high-entropy 9-word portable passphrase")
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("portable-password", help="manage portable BitLocker master passwords")
    psub = p.add_subparsers(dest="pw_action", required=True)
    psub.add_parser("generate", help="generate high-entropy BIP-39 passphrase").set_defaults(func=cmd_portable_password)
    p_add = psub.add_parser("add", help="add portable password to vault (prompts securely)")
    p_add.add_argument("profile")
    p_add.set_defaults(func=cmd_portable_password)
    p_chg = psub.add_parser("change", help="change existing portable password (prompts securely)")
    p_chg.add_argument("profile")
    p_chg.set_defaults(func=cmd_portable_password)
    p_rm = psub.add_parser("remove", help="remove portable password")
    p_rm.add_argument("profile")
    p_rm.set_defaults(func=cmd_portable_password)
    p_ls = psub.add_parser("list", help="list volume BitLocker key protectors")
    p_ls.add_argument("profile")
    p_ls.set_defaults(func=cmd_portable_password)

    p = sub.add_parser("backup", help="manage portable encrypted backups (.svault)")
    bsub = p.add_subparsers(dest="backup_action", required=True)
    p_pub = bsub.add_parser("publish", help="publish verified encrypted backup")
    p_pub.add_argument("profile", nargs="?", default=None)
    p_pub.add_argument("--destination", default=None)
    p_pub.add_argument("--allow-integrity-only", action="store_true",
                       help="allow unauthenticated backup publication without HMAC")
    p_pub.set_defaults(func=cmd_backup)
    p_ver = bsub.add_parser("verify", help="verify backup integrity and manifest HMAC")
    p_ver.add_argument("--destination", default=None)
    p_ver.set_defaults(func=cmd_backup)
    p_opn = bsub.add_parser("open", help="mount arbitrary .svault file (prompts securely for credentials)")
    p_opn.add_argument("vault_path")
    p_opn.add_argument("mount_path")
    p_opn.add_argument("--read-only", action="store_true")
    p_opn.set_defaults(func=cmd_backup)
    p_cls = bsub.add_parser("close", help="dismount portable vault")
    p_cls.add_argument("vault_path")
    p_cls.add_argument("mount_path")
    p_cls.set_defaults(func=cmd_backup)

    p = sub.add_parser("ffs", help="FreeFileSync safety audit and hardening")
    fsub = p.add_subparsers(dest="ffs_action", required=True)
    p_faud = fsub.add_parser("audit", help="audit FreeFileSync configuration and sync destinations")
    p_faud.add_argument("--batch", default=None)
    p_faud.add_argument("--gui", default=None)
    p_faud.add_argument("--output", default=None, help="custom path for leakage inventory report")
    p_faud.set_defaults(func=cmd_ffs)
    p_finv = fsub.add_parser("inventory", help="generate plaintext leakage inventory report")
    p_finv.add_argument("--output", default=None, help="custom path for leakage inventory report")
    p_finv.set_defaults(func=cmd_ffs)
    p_fhrd = fsub.add_parser("harden", help="apply FreeFileSync exclusion hardening with backup")
    p_fhrd.add_argument("--batch", default=None)
    p_fhrd.add_argument("--gui", default=None)
    p_fhrd.add_argument("--force", action="store_true")
    p_fhrd.set_defaults(func=cmd_ffs)
    p_frst = fsub.add_parser("restore", help="restore FreeFileSync configuration from .bak")
    p_frst.add_argument("--batch", default=None)
    p_frst.add_argument("--gui", default=None)
    p_frst.set_defaults(func=cmd_ffs)

    p = sub.add_parser("credential", help="manage enrolled security keys")
    csub = p.add_subparsers(dest="cred_action", required=True)
    p_list = csub.add_parser("list", help="list enrolled security keys")
    p_list.add_argument("profile")
    p_list.set_defaults(func=cmd_credential)

    p_rem = csub.add_parser("remove", help="remove a security key after authenticating with another")
    p_rem.add_argument("profile")
    p_rem.add_argument("credential", help="credential profile label or ID hash")
    p_rem.set_defaults(func=cmd_credential)

    p = sub.add_parser("import-app",
                       help="copy a portable application into managed storage")
    p.add_argument("profile")
    p.add_argument("--source", required=True)
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_import_app)

    p = sub.add_parser("open", help="authenticate, mount and launch, then supervise")
    p.add_argument("profile")
    p.add_argument("--force-auth", action="store_true")
    p.add_argument("--detach", action="store_true",
                   help="do not supervise (leaves the vault mounted)")
    p.add_argument("--poll-seconds", type=float, default=5.0)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("lock", help="secure relock now")
    p.add_argument("profile", nargs="?", default=None)
    p.set_defaults(func=cmd_lock)

    p = sub.add_parser("mode", help="switch the policy mode")
    p.add_argument("profile")
    p.add_argument("mode", choices=sa_config.POLICY_MODES)
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("recover", help="safe relock after an unclean shutdown")
    p.add_argument("profile")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("capacity",
                       help="measured container capacity truth (used logical, "
                            "host free, warnings) and an explicit grow plan "
                            "(--grow-gb N); mounts nothing, resizes nothing "
                            "without operator consent")
    p.add_argument("profile")
    p.add_argument("--grow-gb", type=int, default=None,
                   help="produce the grow plan for this new logical size; "
                        "the plan is printed, never executed")
    p.set_defaults(func=cmd_capacity)

    p = sub.add_parser("emergency",
                       help="standalone emergency container recovery (T-254): "
                            "attach+letter, unlock with the recovery password "
                            "(stdin, hidden), then lock and dismount. "
                            "Refusals stop the sequence; nothing is echoed")
    p.add_argument("profile", nargs="?", default="obsidian")
    p.add_argument("step", choices=["attach", "unlock", "close", "all"])
    p.add_argument("--container", default=None,
                   help="direct path to container image (svault or vhdx), bypassing registry")
    p.add_argument("--letter", default=None,
                   help="drive letter to unlock (from the attach step's output)")
    p.set_defaults(func=cmd_emergency)

    p = sub.add_parser("migrate", help="migrate a plaintext vault into the container "
                                       "(requires a passed hardware acceptance and recovery readiness)")
    p.add_argument("profile")
    p.add_argument("--source", default=None,
                   help="the plaintext vault to migrate (required unless "
                        "--check-acceptance)")
    p.add_argument("--no-app-verify", action="store_true")
    p.add_argument("--restart", action="store_true",
                   help="ignore the journal and start from PRECHECK")
    p.add_argument("--check-acceptance", action="store_true",
                   help="evaluate the hardware-acceptance gate exactly as a "
                        "migration would, then stop: no key, no mount, no copy")
    p.add_argument("--check-readiness", action="store_true",
                   help="evaluate the recovery-readiness gate exactly as a "
                        "migration would, then stop: no key, no mount, no copy")
    p.add_argument("--drill-readiness", action="store_true",
                   help="run disposable restore drill and record recovery readiness proof")
    p.add_argument("--backup-dest", default=None,
                   help="destination directory or file for backup readiness drill")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("migrate-status", help="inspect the migration journal")
    p.add_argument("profile")
    p.set_defaults(func=cmd_migrate_status)

    p = sub.add_parser("migrate-recover", help="repair an interrupted migration")
    p.add_argument("profile")
    p.set_defaults(func=cmd_migrate_recover)

    p = sub.add_parser("privileged-helper",
                       help="install, repair, remove or check the silent "
                            "privileged helper task (setup only: ordinary "
                            "open and lock never prompt)")
    p.add_argument("action", choices=("check", "preflight", "install", "repair",
                                      "remove", "prepare-uac"))
    p.add_argument("--idle-timeout", type=int, default=None,
                   help="seconds an idle elevated helper may live (default %d)"
                        % sa_privtask.DEFAULT_IDLE_TIMEOUT_SECONDS)
    p.add_argument("--start", action="store_true",
                   help="with 'check': also start the helper silently and "
                        "verify it arrives at high integrity")
    p.add_argument("--confirm", action="store_true",
                   help="with 'prepare-uac': really set EnableLUA=1")
    p.add_argument("--apply", action="store_true",
                   help=argparse.SUPPRESS)      # the elevated child's own entry
    p.add_argument("--result", default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_privileged_helper)

    p = sub.add_parser("audit", help="tail the audit log")
    p.add_argument("--count", type=int, default=50)
    p.set_defaults(func=cmd_audit)
    return parser


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except sa_config.ConfigError as exc:
        emit({"ok": False, "error_category": "config_invalid", "message": str(exc)}, True)
        return EXIT_CONFIG
    except sa_paths.PathPolicyError as exc:
        emit({"ok": False, "error_category": "path_policy", "message": str(exc)}, True)
        return EXIT_CONFIG
    except sa_broker.BrokerError as exc:
        emit({"ok": False, "error_category": exc.category, "message": str(exc)}, True)
        return EXIT_FAILED
    except sa_privtask.PrivilegeTaskError as exc:
        emit({"ok": False, "error_category": exc.category, "message": str(exc),
              "token": exc.token}, True)
        return EXIT_FAILED
    except (sa_auth.AuthError, sa_storage.StorageError, sa_applife.AppLaunchError,
            sa_migrate.MigrationError, sa_backup.BackupError, sa_ffs.FFSError) as exc:
        emit({"ok": False,
              "error_category": getattr(exc, "category", "internal"),
              "message": str(exc)}, True)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
