"""Claude profile SAIPEN admission hooks projection and reconciliation.

Preserves unrelated user settings in settings.json.
Never touches or copies authentication material, tokens, credentials, or sessions.
Idempotent: running multiple times produces no second mutation.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

CANONICAL_EVENTS: Tuple[Tuple[str, Optional[str], int], ...] = (
    ("SessionStart", None, 30),
    ("UserPromptSubmit", None, 30),
    ("PostToolUse", "Bash", 30),
    ("PostModelSwitch", None, 30),
    ("Stop", None, 60),
)

GUARD_IDENTIFIER = "saipen-guard.py"


def _is_saipen_hook(hook_def: Any) -> bool:
    if not isinstance(hook_def, dict):
        return False
    cmd = str(hook_def.get("command", ""))
    return GUARD_IDENTIFIER in cmd


def resolve_canonical_claude_hooks(
    primary_dir: Optional[Path] = None,
    saipen_root: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """Resolve the canonical SAIPEN hook specifications.

    Extracts existing SAIPEN hook commands from primary_dir (e.g. ~/.claude/settings.json),
    or constructs the canonical command using active Python and saipen_root.
    """
    if primary_dir is None:
        primary_dir = Path.home() / ".claude"

    primary_settings = Path(primary_dir) / "settings.json"
    extracted_command: Optional[str] = None
    if primary_settings.is_file():
        try:
            doc = json.loads(primary_settings.read_text(encoding="utf-8-sig"))
            hooks_map = doc.get("hooks", {})
            if isinstance(hooks_map, dict):
                for event, groups in hooks_map.items():
                    if not isinstance(groups, list):
                        continue
                    for group in groups:
                        if not isinstance(group, dict):
                            continue
                        for hook in group.get("hooks", []):
                            if _is_saipen_hook(hook):
                                extracted_command = hook.get("command")
                                break
                        if extracted_command:
                            break
                    if extracted_command:
                        break
        except (OSError, ValueError):
            pass

    if not extracted_command:
        # Construct fallback from active system environment
        py_exe = sys.executable.replace("\\", "/")
        guard_path = (Path.home() / ".claude" / "hooks" / "saipen-guard.py").as_posix()
        root_path = (
            Path(saipen_root).as_posix()
            if saipen_root
            else (Path.home() / ".claude" / "skills" / "saipen").as_posix()
        )
        extracted_command = (
            f"{py_exe} -c 'import os,runpy,sys;a=sys.argv[1];sys.argv=sys.argv[1:];"
            f"os.path.isfile(a) and runpy.run_path(a,run_name=__name__)' "
            f"{guard_path} --host claude --saipen-root {root_path}"
        )

    hooks_catalog: Dict[str, Dict[str, Any]] = {}
    for event, matcher, timeout in CANONICAL_EVENTS:
        hooks_catalog[event] = {
            "matcher": matcher,
            "timeout": timeout,
            "hook": {
                "type": "command",
                "command": extracted_command,
                "timeout": timeout,
            },
        }

    return hooks_catalog


def inspect_claude_hooks(
    profile_dir: Path,
    canonical_catalog: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Inspect the status of SAIPEN hooks in a Claude profile directory."""
    profile_dir = Path(profile_dir)
    settings_file = profile_dir / "settings.json"

    if canonical_catalog is None:
        canonical_catalog = resolve_canonical_claude_hooks()

    if not profile_dir.exists():
        return {
            "status": "PROFILE_DIR_MISSING",
            "path": str(settings_file),
            "missing_events": list(canonical_catalog.keys()),
            "drifted_events": [],
            "style_conflicts": [],
        }

    if not settings_file.is_file():
        return {
            "status": "SETTINGS_MISSING",
            "path": str(settings_file),
            "missing_events": list(canonical_catalog.keys()),
            "drifted_events": [],
            "style_conflicts": [],
        }

    try:
        content = settings_file.read_bytes()
        data = json.loads(content.decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        return {
            "status": "MALFORMED_SETTINGS",
            "path": str(settings_file),
            "error": str(exc),
            "missing_events": list(canonical_catalog.keys()),
            "drifted_events": [],
            "style_conflicts": [],
        }

    if not isinstance(data, dict):
        return {
            "status": "MALFORMED_SETTINGS",
            "path": str(settings_file),
            "error": "Root JSON must be an object",
            "missing_events": list(canonical_catalog.keys()),
            "drifted_events": [],
            "style_conflicts": [],
        }

    hooks_obj = data.get("hooks", {})
    if not isinstance(hooks_obj, dict):
        return {
            "status": "MALFORMED_HOOKS",
            "path": str(settings_file),
            "error": "'hooks' must be an object",
            "missing_events": list(canonical_catalog.keys()),
            "drifted_events": [],
            "style_conflicts": [],
        }

    missing_events: List[str] = []
    drifted_events: List[str] = []

    for event, spec in canonical_catalog.items():
        expected_hook = spec["hook"]
        expected_matcher = spec["matcher"]

        groups = hooks_obj.get(event)
        if not isinstance(groups, list):
            missing_events.append(event)
            continue

        found_owned: List[Dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_matcher = group.get("matcher")
            if expected_matcher is not None and group_matcher != expected_matcher:
                continue
            for h in group.get("hooks", []):
                if _is_saipen_hook(h):
                    found_owned.append(h)

        if len(found_owned) == 0:
            missing_events.append(event)
        elif len(found_owned) == 1:
            owned = found_owned[0]
            if (
                owned.get("command") != expected_hook["command"]
                or owned.get("timeout") != expected_hook["timeout"]
                or owned.get("type") != expected_hook["type"]
            ):
                drifted_events.append(event)
        else:
            drifted_events.append(event)

    status = "CURRENT"
    if missing_events:
        status = "MISSING_HOOKS"
    elif drifted_events:
        status = "DRIFTED_HOOKS"

    return {
        "status": status,
        "path": str(settings_file),
        "missing_events": missing_events,
        "drifted_events": drifted_events,
    }


def reconcile_claude_hooks(
    profile_dir: Path,
    canonical_catalog: Optional[Dict[str, Dict[str, Any]]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Idempotently reconcile SAIPEN hooks in profile_dir/settings.json.

    - Preserves all unrelated configuration and hooks.
    - Never reads or modifies credentials/auth files.
    - Safe on nonexistent profile directory (creates it if needed).
    - Fails safely on malformed JSON without corrupting or truncating.
    """
    profile_dir = Path(profile_dir)
    settings_file = profile_dir / "settings.json"

    if canonical_catalog is None:
        canonical_catalog = resolve_canonical_claude_hooks()

    inspection = inspect_claude_hooks(profile_dir, canonical_catalog)
    if inspection["status"] == "CURRENT":
        return {
            "status": "ALREADY_CURRENT",
            "mutated": False,
            "path": str(settings_file),
        }

    if inspection["status"] in ("MALFORMED_SETTINGS", "MALFORMED_HOOKS"):
        return {
            "status": inspection["status"],
            "error": inspection.get("error", "malformed hook configuration"),
            "mutated": False,
            "path": str(settings_file),
        }

    data: Dict[str, Any] = {}
    if settings_file.is_file():
        try:
            content = settings_file.read_bytes()
            data = json.loads(content.decode("utf-8-sig"))
        except (OSError, ValueError) as exc:
            return {
                "status": "MALFORMED_SETTINGS",
                "error": str(exc),
                "mutated": False,
                "path": str(settings_file),
            }

    hooks_obj = data.setdefault("hooks", {})
    if not isinstance(hooks_obj, dict):
        return {
            "status": "MALFORMED_HOOKS",
            "error": "'hooks' is not a JSON object",
            "mutated": False,
            "path": str(settings_file),
        }

    mutated = False

    for event, spec in canonical_catalog.items():
        expected_hook = spec["hook"]
        expected_matcher = spec["matcher"]

        groups = hooks_obj.setdefault(event, [])
        if not isinstance(groups, list):
            groups = []
            hooks_obj[event] = groups
            mutated = True

        target_group: Optional[Dict[str, Any]] = None
        for group in groups:
            if not isinstance(group, dict):
                continue
            if expected_matcher is None:
                if "matcher" not in group or group.get("matcher") is None:
                    target_group = group
                    break
            else:
                if group.get("matcher") == expected_matcher:
                    target_group = group
                    break

        if target_group is None:
            target_group = {"hooks": []}
            if expected_matcher is not None:
                target_group["matcher"] = expected_matcher
            groups.append(target_group)
            mutated = True

        hook_list = target_group.setdefault("hooks", [])
        if not isinstance(hook_list, list):
            hook_list = []
            target_group["hooks"] = hook_list
            mutated = True

        owned_indices = [
            i for i, h in enumerate(hook_list) if _is_saipen_hook(h)
        ]

        if not owned_indices:
            hook_list.append(dict(expected_hook))
            mutated = True
        else:
            first_idx = owned_indices[0]
            current_hook = hook_list[first_idx]
            if (
                current_hook.get("command") != expected_hook["command"]
                or current_hook.get("timeout") != expected_hook["timeout"]
                or current_hook.get("type") != expected_hook["type"]
            ):
                hook_list[first_idx] = dict(expected_hook)
                mutated = True
            # Remove any duplicate owned hooks in the same group
            for dup_idx in reversed(owned_indices[1:]):
                del hook_list[dup_idx]
                mutated = True

    if mutated and not dry_run:
        profile_dir.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        tmp_file = settings_file.with_name(f"{settings_file.name}.tmp.{os.getpid()}")
        try:
            tmp_file.write_bytes(encoded)
            tmp_file.replace(settings_file)
        finally:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass

    return {
        "status": "REPAIRED" if mutated else "ALREADY_CURRENT",
        "mutated": mutated,
        "path": str(settings_file),
    }


def remove_claude_hooks(
    profile_dir: Path,
    active_profile_dirs: Optional[List[Path]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove SAIPEN hooks from profile_dir if no other active account uses it."""
    profile_dir = Path(profile_dir).resolve()
    if active_profile_dirs:
        for other in active_profile_dirs:
            try:
                if other.resolve() == profile_dir:
                    return {
                        "status": "STILL_ACTIVE",
                        "mutated": False,
                        "path": str(profile_dir / "settings.json"),
                    }
            except OSError:
                pass

    settings_file = profile_dir / "settings.json"
    if not settings_file.is_file():
        return {
            "status": "NO_SETTINGS",
            "mutated": False,
            "path": str(settings_file),
        }

    try:
        content = settings_file.read_bytes()
        data = json.loads(content.decode("utf-8-sig"))
    except (OSError, ValueError):
        return {
            "status": "MALFORMED_SETTINGS",
            "mutated": False,
            "path": str(settings_file),
        }

    if not isinstance(data, dict):
        return {"status": "MALFORMED_SETTINGS", "mutated": False}

    hooks_obj = data.get("hooks")
    if not isinstance(hooks_obj, dict):
        return {"status": "NO_HOOKS", "mutated": False}

    mutated = False
    events_to_clean: List[str] = []

    for event, groups in hooks_obj.items():
        if not isinstance(groups, list):
            continue
        cleaned_groups: List[Dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                cleaned_groups.append(group)
                continue
            h_list = group.get("hooks", [])
            kept_hooks = [h for h in h_list if not _is_saipen_hook(h)]
            if len(kept_hooks) != len(h_list):
                mutated = True
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                cleaned_groups.append(new_group)
        hooks_obj[event] = cleaned_groups
        if not cleaned_groups:
            events_to_clean.append(event)

    for ev in events_to_clean:
        del hooks_obj[ev]
        mutated = True

    if not hooks_obj:
        del data["hooks"]
        mutated = True

    if mutated and not dry_run:
        encoded = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        tmp_file = settings_file.with_name(f"{settings_file.name}.tmp.{os.getpid()}")
        try:
            tmp_file.write_bytes(encoded)
            tmp_file.replace(settings_file)
        finally:
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass

    return {
        "status": "REMOVED" if mutated else "ALREADY_CLEAN",
        "mutated": mutated,
        "path": str(settings_file),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", type=Path, required=True, help="Claude profile directory")
    parser.add_argument("--check", action="store_true", help="Only check status without modifying")
    parser.add_argument("--remove", action="store_true", help="Remove SAIPEN hooks from profile")
    args = parser.parse_args()

    if args.check:
        res = inspect_claude_hooks(args.profile_dir)
        print(json.dumps(res, indent=2))
        return 0 if res["status"] == "CURRENT" else 1

    if args.remove:
        res = remove_claude_hooks(args.profile_dir)
        print(json.dumps(res, indent=2))
        return 0

    res = reconcile_claude_hooks(args.profile_dir)
    print(json.dumps(res, indent=2))
    return 0 if res["status"] in ("REPAIRED", "ALREADY_CURRENT") else 1


if __name__ == "__main__":
    raise SystemExit(main())
