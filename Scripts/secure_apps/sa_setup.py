"""Setup orchestration and state evaluation for SAITULS Secure Apps.

Calculates setup progress, ordered checklist states, next required action,
durable reboot tracking, and human-facing diagnostics without secrets.
"""
import ctypes
import json
import ntpath
import os
import sys
import time

import sa_acceptance
import sa_auth
import sa_migrate
import sa_paths
import sa_privtask

# Typed actions returned by get_next_setup_action()
ACTION_ENABLE_PRIVILEGE_SEPARATION = "ENABLE_PRIVILEGE_SEPARATION"
ACTION_REBOOT_REQUIRED = "REBOOT_REQUIRED"
ACTION_RUN_NON_ELEVATED = "RUN_NON_ELEVATED"
ACTION_INSTALL_HELPER = "INSTALL_HELPER"
ACTION_COMMISSION_HELPER = "COMMISSION_HELPER"
ACTION_PROBE_FIDO2 = "PROBE_FIDO2"
ACTION_RUN_ACCEPTANCE = "RUN_ACCEPTANCE"
ACTION_IMPORT_APPLICATION = "IMPORT_APPLICATION"
ACTION_ENROLL_PROFILE = "ENROLL_PROFILE"
ACTION_MIGRATE_VAULT = "MIGRATE_VAULT"
ACTION_READY = "READY"

ALL_SETUP_ACTIONS = (
    ACTION_ENABLE_PRIVILEGE_SEPARATION,
    ACTION_REBOOT_REQUIRED,
    ACTION_RUN_NON_ELEVATED,
    ACTION_INSTALL_HELPER,
    ACTION_COMMISSION_HELPER,
    ACTION_PROBE_FIDO2,
    ACTION_RUN_ACCEPTANCE,
    ACTION_IMPORT_APPLICATION,
    ACTION_ENROLL_PROFILE,
    ACTION_MIGRATE_VAULT,
    ACTION_READY,
)

# Step status tokens
STEP_READY = "READY"
STEP_ACTION_REQUIRED = "ACTION REQUIRED"
STEP_REBOOT_REQUIRED = "REBOOT REQUIRED"
STEP_WAITING = "WAITING"
STEP_RUNNING = "RUNNING"
STEP_FAILED = "FAILED"
STEP_COMPLETE = "COMPLETE"

REBOOT_MARKER_FILENAME = "reboot-pending.json"

#: A marker written during THIS boot session is honoured no matter what the
#: measured privilege state looks like. ``EnableLUA=1`` with a medium-integrity
#: broker is exactly what a PREPARED-BUT-UNRESTARTED host looks like -- it is
#: also what a restarted one looks like -- so that pair may only ever be used
#: as evidence when nothing at all is known about which boot wrote the marker.
REBOOT_IDENTITY_TOLERANCE_SECONDS = 120.0


def _system_uptime_ms():
    """System uptime in milliseconds on Windows, or None elsewhere."""
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            return int(kernel32.GetTickCount64())
        except Exception:
            return None
    return None


def _boot_identity():
    """(uptime_ms, approximate boot wall-clock) or (None, None).

    The wall-clock of the boot itself is the durable evidence, because raw
    uptime can also be LARGER after a restart (mark the marker on day one, turn
    the machine off for a week, come back) and a requirement that is silently
    dropped is worse than one that asks for a second restart.
    """
    uptime_ms = _system_uptime_ms()
    if uptime_ms is None:
        return None, None
    return uptime_ms, time.time() - (uptime_ms / 1000.0)


def mark_reboot_pending(state_dir, pending=True):
    """Save or remove the durable reboot-required marker in state_dir."""
    if not state_dir:
        return
    path = ntpath.join(state_dir, REBOOT_MARKER_FILENAME)
    if not pending:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    os.makedirs(state_dir, exist_ok=True)
    uptime_ms, boot_time = _boot_identity()
    payload = {
        "reboot_pending": True,
        "marked_at": os.name,
        "uptime_ms": uptime_ms,
        "boot_time": boot_time,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def is_reboot_pending(state_dir, preflight_report=None):
    """True if EnableLUA was prepared and the system has not rebooted yet."""
    if not state_dir:
        return False
    path = ntpath.join(state_dir, REBOOT_MARKER_FILENAME)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    if not isinstance(data, dict) or not data.get("reboot_pending"):
        return False

    uptime_now, boot_now = _boot_identity()
    uptime_marked = data.get("uptime_ms")
    boot_marked = data.get("boot_time")

    if uptime_now is not None and uptime_marked is not None:
        if uptime_now < uptime_marked:
            # Uptime went backwards: the machine certainly restarted.
            mark_reboot_pending(state_dir, False)
            return False
        if boot_now is not None and boot_marked is not None:
            try:
                drift = boot_now - float(boot_marked)
            except (TypeError, ValueError):
                drift = None
            if drift is not None and abs(drift) <= REBOOT_IDENTITY_TOLERANCE_SECONDS:
                # The marker was written during THIS boot session, so Windows
                # has not been restarted since. Privilege separation may simply
                # not have been applied yet.
                return True
            if drift is not None and drift > REBOOT_IDENTITY_TOLERANCE_SECONDS:
                # A later boot wrote the current one: the restart really
                # happened, so the requirement is satisfied and the marker is
                # retired.
                mark_reboot_pending(state_dir, False)
                return False

    if preflight_report:
        enable_lua = preflight_report.get("EnableLUA")
        broker_elevated = preflight_report.get("broker_elevated")
        if enable_lua is True and broker_elevated is False:
            # Last resort, for a marker from before boot identity was recorded.
            mark_reboot_pending(state_dir, False)
            return False

    return True


def get_next_setup_action(
    registry,
    profile_id="obsidian",
    preflight_report=None,
    commissioned=False,
    probe_result=None,
    acceptance_verdict=None,
    reboot_pending=None,
    script_dir=None,
    enrolled_fact=None,
    migration_verdict=None,
):
    """Determine the single highlighted next action from real system state.

    Returns one of the ACTION_* constants.
    """
    state_dir = registry.state_dir if registry else None
    if preflight_report is None:
        preflight_report = sa_privtask.preflight(script_dir=script_dir)

    if reboot_pending is None:
        reboot_pending = is_reboot_pending(state_dir, preflight_report)

    enable_lua = preflight_report.get("EnableLUA")
    if enable_lua is False:
        if reboot_pending:
            return ACTION_REBOOT_REQUIRED
        return ACTION_ENABLE_PRIVILEGE_SEPARATION

    if reboot_pending:
        return ACTION_REBOOT_REQUIRED

    broker_elevated = preflight_report.get("broker_elevated")
    broker_integrity = preflight_report.get("broker_integrity")
    if broker_elevated is True or broker_integrity == "HIGH":
        return ACTION_RUN_NON_ELEVATED

    helper_installed = preflight_report.get("scheduled_helper_installed")
    if not helper_installed:
        return ACTION_INSTALL_HELPER

    helper_valid = preflight_report.get("scheduled_helper_definition_valid")
    if not helper_valid:
        return ACTION_COMMISSION_HELPER

    if not commissioned:
        # Check if acceptance record already vouched for silent_privileged_start_accepted
        if acceptance_verdict is None and registry:
            try:
                prof = registry.get(profile_id)
                if prof:
                    acceptance_verdict = sa_acceptance.evaluate(registry.state_dir, prof)
            except Exception:
                acceptance_verdict = None
        record = (
            acceptance_verdict.record
            if (acceptance_verdict and isinstance(acceptance_verdict.record, dict))
            else {}
        )
        if not record.get("silent_privileged_start_accepted"):
            return ACTION_COMMISSION_HELPER

    # Next: Disposable acceptance (if already valid, bypass probe)
    if acceptance_verdict is None and registry:
        try:
            prof = registry.get(profile_id)
            if prof:
                acceptance_verdict = sa_acceptance.evaluate(registry.state_dir, prof)
        except Exception:
            acceptance_verdict = None

    if not acceptance_verdict or not acceptance_verdict.ok:
        if not probe_result or not probe_result.get("available") or not probe_result.get("hmac_secret"):
            return ACTION_PROBE_FIDO2
        return ACTION_RUN_ACCEPTANCE

    # Next: Obsidian application import
    profile = registry.get(profile_id) if registry else None
    if profile:
        app_exe = getattr(profile, "executable", None)
        if not app_exe or not os.path.isfile(app_exe):
            return ACTION_IMPORT_APPLICATION

    # Next: Encrypted vault enrollment
    if registry and profile:
        if enrolled_fact is None:
            store = sa_auth.CredentialStore(registry.credentials_path)
            enrolled_fact = bool(
                store.is_enrolled(profile_id)
                and os.path.isfile(profile.container))
        if not enrolled_fact:
            return ACTION_ENROLL_PROFILE

    # Next: Vault migration
    if registry and profile:
        if migration_verdict is None:
            journal_path = ntpath.join(registry.state_dir, "migration-%s.json" % profile_id)
            migration_verdict = sa_migrate.inspect(journal_path).get("verdict")
        if migration_verdict != "COMPLETE":
            return ACTION_MIGRATE_VAULT

    return ACTION_READY


def get_setup_checklist(
    registry,
    profile_id="obsidian",
    preflight_report=None,
    commissioned=False,
    probe_result=None,
    acceptance_verdict=None,
    reboot_pending=None,
    script_dir=None,
    enrolled_fact=None,
    migration_verdict=None,
):
    """Compute the status and detail for all 8 setup steps.

    Returns a list of dicts:
    [
      {
        "index": 1,
        "title": "Windows privilege separation",
        "status": "READY" | "ACTION REQUIRED" | "REBOOT REQUIRED" | "WAITING" | "COMPLETE" | "FAILED",
        "summary": "...",
        "detail": "..."
      },
      ...
    ]
    """
    state_dir = registry.state_dir if registry else None
    if preflight_report is None:
        preflight_report = sa_privtask.preflight(script_dir=script_dir)

    if reboot_pending is None:
        reboot_pending = is_reboot_pending(state_dir, preflight_report)

    profile = registry.get(profile_id) if registry else None
    if acceptance_verdict is None and registry and profile:
        try:
            acceptance_verdict = sa_acceptance.evaluate(registry.state_dir, profile)
        except Exception:
            acceptance_verdict = None

    steps = []

    # Step 1: Windows privilege separation
    s1_enable = preflight_report.get("EnableLUA")
    s1_elevated = preflight_report.get("broker_elevated")
    if s1_enable is True and s1_elevated is False:
        s1_status = STEP_COMPLETE
        s1_summary = "Privilege separation active (MEDIUM integrity)."
    elif reboot_pending:
        s1_status = STEP_REBOOT_REQUIRED
        s1_summary = "Windows must be restarted to apply privilege separation."
    elif s1_enable is False:
        s1_status = STEP_ACTION_REQUIRED
        s1_summary = "EnableLUA is disabled. Windows privilege separation is off."
    elif s1_elevated is True:
        s1_status = STEP_ACTION_REQUIRED
        s1_summary = "SAITULS is running elevated. Close it and launch normally."
    else:
        s1_status = STEP_ACTION_REQUIRED
        s1_summary = "Privilege separation check required."

    steps.append({
        "index": 1,
        "title": "Windows privilege separation",
        "status": s1_status,
        "summary": s1_summary,
    })

    if s1_status == STEP_REBOOT_REQUIRED:
        wait_titles = [
            (2, "Silent privileged helper"),
            (3, "Privileged helper verification"),
            (4, "YubiKey probe"),
            (5, "Disposable security acceptance"),
            (6, "Obsidian application"),
            (7, "Encrypted vault enrollment"),
            (8, "Vault migration"),
        ]
        for idx, title in wait_titles:
            steps.append({
                "index": idx,
                "title": title,
                "status": STEP_WAITING,
                "summary": "Restart Windows to continue.",
            })
        return steps

    # Step 2: Silent privileged helper
    if s1_status != STEP_COMPLETE:
        s2_status = STEP_WAITING
        s2_summary = "Waiting for Windows privilege separation."
    elif preflight_report.get("scheduled_helper_installed"):
        s2_status = STEP_COMPLETE
        s2_summary = "Privileged helper task is installed."
    else:
        s2_status = STEP_ACTION_REQUIRED
        s2_summary = "Helper task not installed. Requires one-time Windows approval."

    steps.append({
        "index": 2,
        "title": "Silent privileged helper",
        "status": s2_status,
        "summary": s2_summary,
    })

    # Step 3: Privileged helper verification
    record = (
        acceptance_verdict.record
        if (acceptance_verdict and isinstance(acceptance_verdict.record, dict))
        else {}
    )
    is_comm = commissioned or bool(record.get("silent_privileged_start_accepted"))
    if s2_status != STEP_COMPLETE:
        s3_status = STEP_WAITING
        s3_summary = "Waiting for privileged helper install."
    elif is_comm:
        s3_status = STEP_COMPLETE
        s3_summary = "Silent privileged start verified."
    elif preflight_report.get("scheduled_helper_definition_valid"):
        s3_status = STEP_READY
        s3_summary = "Task definition valid. Ready for silent start test."
    else:
        s3_status = STEP_FAILED
        s3_summary = "Task definition mismatch or tampered. Repair required."

    steps.append({
        "index": 3,
        "title": "Privileged helper verification",
        "status": s3_status,
        "summary": s3_summary,
    })

    # Step 4: YubiKey probe
    probe_ok = bool(
        probe_result
        and probe_result.get("available")
        and probe_result.get("hmac_secret")
    )
    acc_ok = bool(acceptance_verdict and acceptance_verdict.ok)
    if s3_status != STEP_COMPLETE:
        s4_status = STEP_WAITING
        s4_summary = "Waiting for helper verification."
    elif probe_ok or acc_ok:
        s4_status = STEP_COMPLETE
        s4_summary = (
            "FIDO2 security key detected (hmac-secret supported)."
            if probe_ok
            else "Durable acceptance verified FIDO2 security key."
        )
    else:
        s4_status = STEP_READY
        s4_summary = "Probe required to detect connected security key."

    steps.append({
        "index": 4,
        "title": "YubiKey probe",
        "status": s4_status,
        "summary": s4_summary,
    })

    # Step 5: Disposable security acceptance
    if s4_status != STEP_COMPLETE:
        s5_status = STEP_WAITING
        s5_summary = "Waiting for YubiKey probe."
    elif acc_ok:
        s5_status = STEP_COMPLETE
        s5_summary = "Hardware acceptance passed. Migration gate ready."
    else:
        s5_status = STEP_READY
        s5_summary = "Disposable acceptance test required before protecting real data."

    steps.append({
        "index": 5,
        "title": "Disposable security acceptance",
        "status": s5_status,
        "summary": s5_summary,
    })

    # Step 6: Obsidian application
    app_exe = getattr(profile, "executable", None) if profile else None
    app_ok = bool(app_exe and os.path.isfile(app_exe))
    if s5_status != STEP_COMPLETE:
        s6_status = STEP_WAITING
        s6_summary = "Waiting for disposable security acceptance."
    elif app_ok:
        s6_status = STEP_COMPLETE
        s6_summary = "Obsidian portable application imported."
    else:
        s6_status = STEP_READY
        s6_summary = "Portable Obsidian not imported into managed storage."

    steps.append({
        "index": 6,
        "title": "Obsidian application",
        "status": s6_status,
        "summary": s6_summary,
    })

    # Step 7: Encrypted vault enrollment
    if enrolled_fact is None:
        store = (
            sa_auth.CredentialStore(registry.credentials_path)
            if registry
            else None
        )
        enrolled_fact = bool(
            store
            and profile
            and store.is_enrolled(profile_id)
            and os.path.isfile(profile.container)
        )
    enrolled = enrolled_fact
    if s6_status != STEP_COMPLETE or s5_status != STEP_COMPLETE:
        s7_status = STEP_WAITING
        s7_summary = "Waiting for application import and acceptance."
    elif enrolled:
        s7_status = STEP_COMPLETE
        s7_summary = "Vault container created and YubiKey enrolled."
    else:
        s7_status = STEP_READY
        s7_summary = "Ready to create encrypted VHDX and enroll first key."

    steps.append({
        "index": 7,
        "title": "Encrypted vault enrollment",
        "status": s7_status,
        "summary": s7_summary,
    })

    # Step 8: Vault migration
    if migration_verdict is None:
        journal_path = (
            ntpath.join(registry.state_dir, "migration-%s.json" % profile_id)
            if registry
            else None
        )
        journal_data = (
            sa_migrate.inspect(journal_path) if journal_path else {}
        )
        migration_verdict = journal_data.get("verdict")
    mig_verdict = migration_verdict
    if s7_status != STEP_COMPLETE:
        s8_status = STEP_WAITING
        s8_summary = "Waiting for vault enrollment."
    elif mig_verdict == "COMPLETE":
        s8_status = STEP_COMPLETE
        s8_summary = "Vault migration completed and verified."
    elif acc_ok:
        s8_status = STEP_READY
        s8_summary = "Migration ready. Real notes can now be migrated."
    else:
        s8_status = STEP_WAITING
        s8_summary = "Hardware acceptance required before migration."

    steps.append({
        "index": 8,
        "title": "Vault migration",
        "status": s8_status,
        "summary": s8_summary,
    })

    return steps


def format_diagnostics(
    registry,
    profile_id="obsidian",
    preflight_report=None,
    acceptance_verdict=None,
    probe_result=None,
    status_payload=None,
    script_dir=None,
):
    """Build a comprehensive, safe, secret-free diagnostics report text."""
    if preflight_report is None:
        preflight_report = sa_privtask.preflight(script_dir=script_dir)

    profile = registry.get(profile_id) if registry else None
    if acceptance_verdict is None and registry and profile:
        try:
            acceptance_verdict = sa_acceptance.evaluate(registry.state_dir, profile)
        except Exception:
            acceptance_verdict = None

    lines = [
        "=== SAITULS SECURE APPS DIAGNOSTICS ===",
        "Schema: saituls.secure-apps/1",
        "",
        "--- Host & Privilege Separation ---",
        "EnableLUA: %s" % preflight_report.get("EnableLUA"),
        "ConsentPromptBehaviorAdmin: %s" % preflight_report.get("ConsentPromptBehaviorAdmin"),
        "PromptOnSecureDesktop: %s" % preflight_report.get("PromptOnSecureDesktop"),
        "broker_integrity: %s" % preflight_report.get("broker_integrity"),
        "broker_elevated: %s" % preflight_report.get("broker_elevated"),
        "user_is_administrator: %s" % preflight_report.get("user_is_administrator"),
        "admin_approval_mode: %s" % preflight_report.get("admin_approval_mode"),
        "",
        "--- Silent Privileged Helper Task ---",
        "scheduled_helper_installed: %s" % preflight_report.get("scheduled_helper_installed"),
        "scheduled_helper_definition_valid: %s" % preflight_report.get("scheduled_helper_definition_valid"),
        "scheduled_helper_runnable: %s" % preflight_report.get("scheduled_helper_runnable"),
        "task_path: %s" % preflight_report.get("task_path"),
        "runtime_acl_valid: %s" % preflight_report.get("runtime_acl_valid"),
        "task_security_valid: %s" % preflight_report.get("task_security_valid"),
        "pin_path: %s" % preflight_report.get("pin_path"),
        "pin_writable_by_user: %s" % preflight_report.get("pin_writable_by_user"),
        "",
        "--- Authentication & Hardware ---",
    ]

    if probe_result:
        lines.extend([
            "fido_detected: %s" % probe_result.get("available"),
            "hmac_secret: %s" % probe_result.get("hmac_secret"),
            "user_verification: %s" % probe_result.get("user_verification"),
            "transport: %s" % probe_result.get("transport"),
            "fido_library: %s" % probe_result.get("library_version"),
        ])
    else:
        lines.append("fido_probe: not performed in this session")

    lines.extend([
        "",
        "--- Acceptance Gate ---",
        "acceptance_valid: %s" % (acceptance_verdict.ok if acceptance_verdict else False),
    ])
    if acceptance_verdict:
        lines.append("acceptance_record: %s" % acceptance_verdict.path)
        if acceptance_verdict.reasons:
            lines.append("acceptance_reasons: %s" % "; ".join(acceptance_verdict.reasons))

    lines.extend([
        "",
        "--- Profile & Storage ---",
        "profile_id: %s" % profile_id,
    ])
    if profile:
        lines.extend([
            "mount_path: %s" % profile.mount_path,
            "container: %s" % profile.container,
            "backend: %s" % profile.backend,
            "policy_mode: %s" % profile.policy.mode,
            "idle_timeout_minutes: %s" % profile.policy.idle_timeout_minutes,
        ])

    if status_payload:
        lines.extend([
            "",
            "--- Current Runtime Status ---",
            "state: %s" % status_payload.get("state"),
            "vault: %s" % status_payload.get("vault_state"),
            "session: %s" % status_payload.get("session_state"),
            "app_running: %s" % status_payload.get("app_running"),
            "last_lock_reason: %s" % status_payload.get("last_lock_reason"),
        ])

    lines.append("")
    lines.append("=== END DIAGNOSTICS ===")
    return "\n".join(lines)


def human_error_message(category, raw_error=""):
    """Translate raw internal error tokens into human-actionable guidance."""
    combined = (str(category or "") + " " + str(raw_error or "")).upper()
    cat = (category or "").lower()

    if "ENABLELUA" in combined:
        return (
            "Windows privilege separation is disabled.\n"
            "Enable it in Step 1 and restart Windows."
        )
    if "BROKER_HIGH_INTEGRITY" in combined or "MUST RUN MEDIUM" in combined:
        return (
            "SAITULS is running as Administrator.\n"
            "Close it and launch normally as a standard process."
        )
    if "PRIVILEGED_TASK_TAMPERED" in combined or "TAMPERED" in combined:
        return (
            "The privileged helper installation no longer matches its trusted configuration.\n"
            "Run 'Repair' in Step 3."
        )
    if "PRIVILEGED_HELPER_NOT_INSTALLED" in combined:
        return (
            "The silent privileged helper is not installed.\n"
            "Click 'Install' in Step 2."
        )
    if "HARDWARE_ACCEPTANCE_REQUIRED" in combined or "MIGRATION_BLOCKED" in combined:
        return (
            "Run the disposable YubiKey security test in Step 5 before protecting real data."
        )
    if "auth_cancelled" in cat:
        return "Operation cancelled: PIN prompt was dismissed or timed out."
    if "auth_wrong_credential" in cat or "wrong credential" in combined.lower():
        return "Incorrect security key presented. Insert the enrolled key."
    if "storage_busy" in cat:
        return "Protected storage is busy. Wait for existing operations to finish."
    if "not_enrolled" in cat:
        return "No enrolled security key found. Complete Step 7 first."

    return str(raw_error or "") or "An unexpected error occurred."
