"""Authoritative local storage protector layout records for SAITULS Secure Apps (R3).

Persists the exact role assignments for BitLocker protectors:
- daily protector: RecoveryPassword (or legacy Password) ID & type
- portable protector: Password ID & type (if configured)
- break-glass protector: RecoveryPassword ID & type

NEVER persists protector secrets or keys.
Binds strictly to profile.container_id and container_path.
"""
import json
import os
import tempfile
import time


LAYOUT_SCHEMA = "saituls.secure-apps.protector-layout/1"
LAYOUT_VERSION = 2

PROTECTOR_LAYOUT_UNKNOWN = "PROTECTOR_LAYOUT_UNKNOWN"
PROTECTOR_LAYOUT_DRIFT = "PROTECTOR_LAYOUT_DRIFT"
PROTECTOR_RECONCILIATION_REQUIRED = "PROTECTOR_RECONCILIATION_REQUIRED"


class LayoutError(Exception):
    """Raised when protector layout is missing, invalid, or disagrees with native BitLocker."""
    def __init__(self, message, category=PROTECTOR_LAYOUT_UNKNOWN):
        super().__init__(message)
        self.message = message
        self.category = category


def layout_path_for_profile(profile):
    """Return the canonical path for the profile's protector layout file."""
    container = getattr(profile, "container", None)
    if not container:
        raise LayoutError("profile has no container path", PROTECTOR_LAYOUT_UNKNOWN)
    return container + ".layout.json"


def load_layout(profile):
    """Load and validate the protector layout record for a profile.

    Returns the layout dict, or None if layout file does not exist.
    Raises LayoutError if layout file is corrupt or container_id does not match.
    """
    path = layout_path_for_profile(profile)
    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise LayoutError(f"Corrupt protector layout file: {exc}", PROTECTOR_LAYOUT_UNKNOWN)

    if not isinstance(data, dict):
        raise LayoutError("Protector layout must be a JSON object", PROTECTOR_LAYOUT_UNKNOWN)

    schema = data.get("schema")
    if schema != LAYOUT_SCHEMA:
        raise LayoutError(f"Unsupported layout schema: {schema}", PROTECTOR_LAYOUT_UNKNOWN)

    expected_cid = getattr(profile, "container_id", "")
    recorded_cid = data.get("container_id", "")
    if expected_cid and recorded_cid and expected_cid != recorded_cid:
        raise LayoutError(
            f"Layout container_id mismatch: recorded {recorded_cid} != expected {expected_cid}",
            PROTECTOR_LAYOUT_DRIFT
        )

    return data


def save_layout(profile, daily_id, daily_type, break_glass_id, break_glass_type,
                portable_id=None, portable_type="Password"):
    """Atomically write the authoritative protector layout record."""
    path = layout_path_for_profile(profile)
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

    data = {
        "schema": LAYOUT_SCHEMA,
        "layout_version": LAYOUT_VERSION,
        "container_id": getattr(profile, "container_id", ""),
        "container_path": getattr(profile, "container", ""),
        "updated_at": time.time(),
        "daily_protector": {
            "id": str(daily_id).strip() if daily_id else None,
            "type": str(daily_type).strip() if daily_type else "RecoveryPassword"
        },
        "break_glass_protector": {
            "id": str(break_glass_id).strip() if break_glass_id else None,
            "type": str(break_glass_type).strip() if break_glass_type else "RecoveryPassword"
        },
        "portable_protector": {
            "id": str(portable_id).strip() if portable_id else None,
            "type": str(portable_type).strip() if portable_type else "Password"
        } if portable_id else None
    }

    # Atomic write via temp file
    tmp_path = path + f".{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)
    return data


def update_portable_protector(profile, portable_id, portable_type="Password"):
    """Update or remove the portable protector role in the layout record."""
    data = load_layout(profile)
    if not data:
        raise LayoutError("Cannot update portable protector: layout record missing", PROTECTOR_LAYOUT_UNKNOWN)

    if portable_id:
        data["portable_protector"] = {
            "id": str(portable_id).strip(),
            "type": str(portable_type).strip()
        }
    else:
        data["portable_protector"] = None
    data["updated_at"] = time.time()

    path = layout_path_for_profile(profile)
    tmp_path = path + f".{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)
    return data


def verify_layout_against_native(layout, native_protectors):
    """Verify that layout role assignments match the live BitLocker protector list.

    native_protectors: list of dicts with 'id' and 'type' keys.
    Raises LayoutError(..., PROTECTOR_LAYOUT_DRIFT) on any mismatch.
    Returns dict summarizing verified roles.
    """
    if not layout or not isinstance(layout, dict):
        raise LayoutError("Protector layout missing or not a dictionary", PROTECTOR_LAYOUT_UNKNOWN)

    native_by_id = {}
    for p in native_protectors:
        pid = str(p.get("id", "")).strip().lower()
        ptype = str(p.get("type", "")).strip().lower()
        if pid:
            native_by_id[pid] = ptype

    # Verify daily protector
    daily = layout.get("daily_protector")
    if not daily or not daily.get("id"):
        raise LayoutError("Layout lacks daily_protector definition", PROTECTOR_LAYOUT_UNKNOWN)
    daily_id = daily["id"].lower()
    if daily_id not in native_by_id:
        raise LayoutError(
            f"Daily protector {daily['id']} missing from native BitLocker list",
            PROTECTOR_LAYOUT_DRIFT
        )
    if native_by_id[daily_id] != daily.get("type", "").lower():
        raise LayoutError(
            f"Daily protector {daily['id']} type mismatch: native {native_by_id[daily_id]} != layout {daily.get('type')}",
            PROTECTOR_LAYOUT_DRIFT
        )

    # Verify break-glass protector
    bg = layout.get("break_glass_protector")
    if not bg or not bg.get("id"):
        raise LayoutError("Layout lacks break_glass_protector definition", PROTECTOR_LAYOUT_UNKNOWN)
    bg_id = bg["id"].lower()
    if bg_id not in native_by_id:
        raise LayoutError(
            f"Break-glass protector {bg['id']} missing from native BitLocker list",
            PROTECTOR_LAYOUT_DRIFT
        )
    if native_by_id[bg_id] != bg.get("type", "").lower():
        raise LayoutError(
            f"Break-glass protector {bg['id']} type mismatch: native {native_by_id[bg_id]} != layout {bg.get('type')}",
            PROTECTOR_LAYOUT_DRIFT
        )

    # Verify portable protector if declared
    port = layout.get("portable_protector")
    port_present = False
    if port and port.get("id"):
        port_id = port["id"].lower()
        if port_id not in native_by_id:
            raise LayoutError(
                f"Portable protector {port['id']} missing from native BitLocker list",
                PROTECTOR_LAYOUT_DRIFT
            )
        if native_by_id[port_id] != port.get("type", "").lower():
            raise LayoutError(
                f"Portable protector {port['id']} type mismatch: native {native_by_id[port_id]} != layout {port.get('type')}",
                PROTECTOR_LAYOUT_DRIFT
            )
        port_present = True

    return {
        "daily_present": True,
        "daily_id": daily["id"],
        "break_glass_present": True,
        "break_glass_id": bg["id"],
        "portable_present": port_present,
        "portable_id": port["id"] if port_present else None
    }


def journal_path_for_profile(profile):
    """Return the path for the profile's protector mutation journal."""
    container = getattr(profile, "container", None)
    if not container:
        raise LayoutError("profile has no container path", PROTECTOR_LAYOUT_UNKNOWN)
    return container + ".mutation_journal.json"


def record_mutation_start(profile, operation_id, operation_type, old_roles, target_roles, stage="STARTED"):
    """Atomically record the beginning of a protector mutation transaction."""
    path = journal_path_for_profile(profile)
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    record = {
        "operation_id": str(operation_id),
        "container_id": getattr(profile, "container_id", ""),
        "operation_type": str(operation_type),
        "old_roles": old_roles,
        "target_roles": target_roles,
        "stage": str(stage),
        "timestamp": time.time(),
    }
    tmp_path = path + f".{operation_id}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp_path, path)
    return record


def record_mutation_stage(profile, operation_id, stage):
    """Update the stage of an in-flight mutation journal."""
    path = journal_path_for_profile(profile)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    data["stage"] = str(stage)
    data["updated_at"] = time.time()
    tmp_path = path + f".{operation_id}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)
    return data


def record_mutation_complete(profile, operation_id=None):
    """Remove the journal upon successful completion and layout commit."""
    path = journal_path_for_profile(profile)
    if os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def load_mutation_journal(profile):
    """Load the current mutation journal if one exists."""
    path = journal_path_for_profile(profile)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def reconcile_protector_journal(profile, native_protectors):
    """Reconcile in-flight mutation journal against native BitLocker state on startup/recovery (H5).

    Returns:
      dict with reconciliation result, or None if no journal exists.
    Raises:
      LayoutError(category=PROTECTOR_RECONCILIATION_REQUIRED) if state is inconsistent.
    """
    journal = load_mutation_journal(profile)
    if not journal:
        return None

    native_ids = set()
    for p in (native_protectors or []):
        pid = p.get("id")
        if pid:
            native_ids.add(str(pid).lower())

    target_roles = journal.get("target_roles") or {}
    old_roles = journal.get("old_roles") or {}
    stage = journal.get("stage", "UNKNOWN")

    target_portable = target_roles.get("portable_id")
    target_matches = True
    if target_portable:
        if str(target_portable).lower() not in native_ids:
            target_matches = False
    else:
        # If target was removal, ensure portable is not in native_ids
        old_portable = old_roles.get("portable_id")
        if old_portable and str(old_portable).lower() in native_ids:
            target_matches = False

    if target_matches:
        # Native mutation succeeded before crash, commit target layout
        update_portable_protector(
            profile,
            target_roles.get("portable_id"),
            target_roles.get("portable_type", "Password")
        )
        record_mutation_complete(profile)
        return {
            "status": "RECONCILED_COMMITTED",
            "operation_id": journal.get("operation_id"),
            "stage": stage
        }

    old_portable = old_roles.get("portable_id")
    old_matches = True
    if old_portable:
        if str(old_portable).lower() not in native_ids:
            old_matches = False

    if old_matches:
        # Native mutation did not happen or was fully rolled back
        update_portable_protector(
            profile,
            old_roles.get("portable_id"),
            old_roles.get("portable_type", "Password")
        )
        record_mutation_complete(profile)
        return {
            "status": "RECONCILED_ROLLED_BACK",
            "operation_id": journal.get("operation_id"),
            "stage": stage
        }

    # Interrupted in an unresolvable state
    raise LayoutError(
        f"Interrupted protector mutation ({stage}) detected. Native state requires manual or deterministic reconciliation.",
        PROTECTOR_RECONCILIATION_REQUIRED
    )
