r"""FreeFileSync (FFS) Integration and Safety Guard for SAITULS Secure Apps.

Enforces the ciphertext-only backup boundary:
1. Audits main_sync.ffs_batch and main_sync.ffs_gui to ensure:
   - The plaintext vault mount tree (\__STORE\) is strictly EXCLUDED.
   - The encrypted backup (.svault) is INCLUDED.
   - In-flight partial staging (*.partial) is EXCLUDED.
2. Audits all sync target destinations for historical plaintext leakage.
   NEVER automatically deletes plaintext copies; generates an inventory report
   for operator review and authorization.
3. Applies transactional XML updates to FFS configurations with automatic .bak backup.
"""
import json
import logging
import os
import shutil
import time
import xml.etree.ElementTree as ET

logger = logging.getLogger("saituls.secure_apps.ffs")

DEFAULT_FFS_DIR = r"V:\___VAC\__K\__CONFIG\_FFS"
DEFAULT_BATCH_PATH = os.path.join(DEFAULT_FFS_DIR, "main_sync.ffs_batch")
DEFAULT_GUI_PATH = os.path.join(DEFAULT_FFS_DIR, "main_sync.ffs_gui")

KNOWN_DESTINATIONS = [
    r"R:\__SAVE_R\__K",
    r"G:\__SAVE_G\__K",
    r"E:\__SAVE_E\__K",
    r"S:\__SAVE_S\__K",
    r"M:\__SAVE_M\__K",
    r"K:\__SAVE_K\__K",
    r"P:\__SAVE_P\_GDRIVE_SYNC\__K",
]


class FFSError(Exception):
    category = "ffs_error"

    def __init__(self, message, category=None):
        super().__init__(message)
        if category:
            self.category = category


def locate_authoritative_filter(root):
    """Locate the authoritative global filter element and its parent element.

    FreeFileSync schemas:
      1. Wrapped schema: <FreeFileSync><MainConfig><Filter>...</Filter></MainConfig></FreeFileSync>
      2. Flat schema (XmlFormat="23"+): <FreeFileSync><Filter>...</Filter></FreeFileSync>

    Returns:
        (filter_el, parent_el, schema_type)
        where schema_type is 'main_config' or 'root'.
    Raises:
        FFSError if root element is unrecognized.
    """
    if root.tag != "FreeFileSync":
        raise FFSError(f"FFS_SCHEMA_UNSUPPORTED: Expected <FreeFileSync> root, got <{root.tag}>", "schema_unsupported")

    main_config = root.find("MainConfig")
    if main_config is not None:
        return main_config.find("Filter"), main_config, "main_config"

    # Flat schema
    filter_el = root.find("Filter")
    if (filter_el is not None or
        root.find("FolderPairs") is not None or
        root.attrib.get("XmlType") is not None or
        root.attrib.get("XmlFormat") is not None or
        root.attrib.get("XmlVersion") is not None):
        return filter_el, root, "root"

    raise FFSError("FFS_SCHEMA_UNSUPPORTED: Unrecognized FreeFileSync XML structure", "schema_unsupported")


def find_relevant_folder_pairs(root, target_paths=("V:\\___VAC\\__K", "__K")):
    """Find folder pairs syncing the vault root, plus any pair-level custom filters."""
    folder_pairs_el = root.find("MainConfig/FolderPairs")
    if folder_pairs_el is None:
        folder_pairs_el = root.find("FolderPairs")

    pairs = []
    if folder_pairs_el is not None:
        for pair in folder_pairs_el.findall("Pair"):
            left = (pair.findtext("Left") or "").strip()
            right = (pair.findtext("Right") or "").strip()
            matches = any(t.lower() in left.lower() or t.lower() in right.lower() for t in target_paths)
            if matches:
                pair_filter = pair.find("Filter")
                p_exc = []
                p_inc = []
                if pair_filter is not None:
                    exc_el = pair_filter.find("Exclude")
                    if exc_el is not None:
                        p_exc = [item.text for item in exc_el.findall("Item") if item.text]
                    inc_el = pair_filter.find("Include")
                    if inc_el is not None:
                        p_inc = [item.text for item in inc_el.findall("Item") if item.text]
                pairs.append({
                    "left": left,
                    "right": right,
                    "element": pair,
                    "has_custom_filter": pair_filter is not None,
                    "filter_element": pair_filter,
                    "includes": p_inc,
                    "excludes": p_exc
                })
    return pairs


def parse_ffs_filter(xml_path):
    """Parse an FFS batch or GUI file and return its authoritative filter items."""
    if not os.path.isfile(xml_path):
        raise FFSError(f"FFS file not found: {xml_path}", "file_not_found")

    try:
        tree = ET.parse(xml_path)
    except Exception as exc:
        raise FFSError(f"FFS XML parse error in {xml_path}: {exc}", "xml_corrupt")

    root = tree.getroot()
    filter_el, parent_el, schema_type = locate_authoritative_filter(root)

    includes = []
    excludes = []
    if filter_el is not None:
        inc_el = filter_el.find("Include")
        exc_el = filter_el.find("Exclude")
        if inc_el is not None:
            includes = [item.text for item in inc_el.findall("Item") if item.text]
        if exc_el is not None:
            excludes = [item.text for item in exc_el.findall("Item") if item.text]

    relevant_pairs = find_relevant_folder_pairs(root)

    return {
        "includes": includes,
        "excludes": excludes,
        "relevant_pairs": relevant_pairs,
        "schema_type": schema_type,
        "filter_el": filter_el,
        "parent_el": parent_el,
        "tree": tree,
        "root": root
    }


def audit_ffs_configuration(batch_path=None, gui_path=None):
    """Audit FFS config files for plaintext exposure and ciphertext inclusion."""
    batch_file = batch_path or DEFAULT_BATCH_PATH
    gui_file = gui_path or DEFAULT_GUI_PATH

    results = {}
    existing_count = 0

    for label, path in [("batch", batch_file), ("gui", gui_file)]:
        if not os.path.isfile(path):
            results[label] = {
                "exists": False,
                "safe": False,
                "path": path,
                "error": f"File not found: {path}"
            }
            continue

        existing_count += 1
        try:
            parsed = parse_ffs_filter(path)
        except FFSError as exc:
            results[label] = {
                "exists": True,
                "safe": False,
                "path": path,
                "error": str(exc),
                "schema_unsupported": getattr(exc, "category", "") == "schema_unsupported"
            }
            continue

        excludes = [x.strip().lower() for x in parsed["excludes"] if x]
        includes = [x.strip() for x in parsed["includes"] if x]

        # Check if \__STORE\ or *\__STORE\ is excluded in global filter
        global_store_excluded = any(
            x in ("\\__store\\", "*\\__store\\", "__store\\", "\\__store", "*\\__store")
            for x in excludes
        )

        # Check if any pair-level custom filter overrides or leaks
        pair_leakage = False
        for pair in parsed.get("relevant_pairs", []):
            if pair["has_custom_filter"]:
                p_exc = [x.strip().lower() for x in pair["excludes"] if x]
                p_excluded = any(
                    x in ("\\__store\\", "*\\__store\\", "__store\\", "\\__store", "*\\__store")
                    for x in p_exc
                )
                if not p_excluded:
                    pair_leakage = True
                    break

        store_excluded = global_store_excluded and (not pair_leakage)

        # Check if *.svault is accidentally excluded
        svault_excluded = any(".svault" in x for x in excludes)

        # Evaluate actual Include semantics (do not assume not excluded == included)
        is_included = ("*" in includes) or any(".svault" in x.lower() or "__secure_backups" in x.lower() for x in includes)
        svault_included = is_included and (not svault_excluded)

        # Check if *.partial staging is excluded
        partial_excluded = any("*.partial" in x or ".partial" in x for x in excludes)

        safe = store_excluded and svault_included and partial_excluded

        results[label] = {
            "exists": True,
            "path": path,
            "schema_type": parsed.get("schema_type"),
            "store_excluded": store_excluded,
            "svault_included": svault_included,
            "partial_excluded": partial_excluded,
            "pair_leakage": pair_leakage,
            "safe": safe,
            "total_excludes": len(excludes)
        }

    # Fail closed: if zero files exist, or if ANY checked file is missing or unsafe -> overall_safe = False!
    if existing_count == 0:
        overall_safe = False
    else:
        overall_safe = all(r.get("safe", False) for r in results.values())

    return {
        "overall_safe": overall_safe,
        "details": results
    }


def audit_plaintext_leakage_on_destinations(destinations=None, max_files=1000, include_samples=False):
    """Scan all target destinations for existing plaintext __STORE folders.

    NEVER deletes anything automatically.
    Metadata-hygiene: Does NOT collect sample filenames or inner directories unless explicitly requested (R8).
    """
    dests = destinations or KNOWN_DESTINATIONS
    inventory = {}

    for dest in dests:
        store_path = os.path.join(dest, "__STORE")
        if not os.path.exists(dest):
            inventory[dest] = {"accessible": False, "store_exists": False, "reason": "destination drive/path not available"}
            continue

        if not os.path.exists(store_path):
            inventory[dest] = {"accessible": True, "store_exists": False, "clean": True}
            continue

        file_count = 0
        total_size = 0
        samples = []
        top_entries = []
        try:
            if include_samples:
                try:
                    with os.scandir(store_path) as it:
                        for entry in it:
                            top_entries.append(entry.name)
                except OSError:
                    pass

            for root, dirs, files in os.walk(store_path):
                for f in files:
                    file_count += 1
                    fp = os.path.join(root, f)
                    try:
                        sz = os.path.getsize(fp)
                        total_size += sz
                    except OSError:
                        pass
                    if include_samples and len(samples) < 5:
                        samples.append(os.path.relpath(fp, store_path))
                    if file_count >= max_files:
                        break
                if file_count >= max_files:
                    break
        except OSError as exc:
            inventory[dest] = {
                "accessible": True,
                "store_exists": True,
                "clean": False,
                "error": f"Error walking directory: {exc}"
            }
            continue

        item = {
            "accessible": True,
            "store_exists": True,
            "clean": False,
            "store_path": store_path,
            "file_count": file_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "scan_capped": file_count >= max_files,
            "action_required": "manual_review_and_cleanup_needed"
        }
        if include_samples:
            item["top_entries"] = top_entries
            item["sample_files"] = samples
        inventory[dest] = item

    return inventory


def apply_ffs_hardening(batch_path=None, gui_path=None, backup_suffix=".bak", force=False, require_all=True):
    """Safely update FFS configs to exclude \\__STORE\\ and ensure *.partial is excluded.

    Safety:
    - Fails closed if target files are missing (R7).
    - Locates authoritative Filter node (under MainConfig if wrapped, under root if flat).
    - NEVER creates stray duplicate root-level ./Filter.
    - Fails closed on unsupported schema.
    - Backs up original files to <path>.bak.<timestamp> and <path>.bak.
    - Mandatory re-audit after modification: returns ok=True ONLY if overall_safe is True.
    """
    batch_file = batch_path or DEFAULT_BATCH_PATH
    gui_file = gui_path or DEFAULT_GUI_PATH
    targets = [batch_file, gui_file]
    existing = [p for p in targets if os.path.isfile(p)]

    if not existing:
        return {
            "ok": False,
            "overall_safe": False,
            "error": "No target FFS files found",
            "modified": []
        }

    if require_all and len(existing) < len(targets):
        missing = [p for p in targets if not os.path.isfile(p)]
        return {
            "ok": False,
            "overall_safe": False,
            "error": f"Required FFS configuration files missing: {missing}",
            "modified": []
        }

    modified_files = []

    for path in existing:
        try:
            tree = ET.parse(path)
        except Exception as exc:
            raise FFSError(f"Cannot parse XML {path}: {exc}", "xml_corrupt")

        root = tree.getroot()
        filter_el, parent_el, schema_type = locate_authoritative_filter(root)
        if filter_el is None and parent_el is None:
            return {
                "ok": False,
                "overall_safe": False,
                "error": f"FFS_SCHEMA_UNSUPPORTED in {path}",
                "modified": []
            }

        if filter_el is None:
            filter_el = ET.SubElement(parent_el, "Filter")

        exc_el = filter_el.find("Exclude")
        if exc_el is None:
            exc_el = ET.SubElement(filter_el, "Exclude")

        existing_items = [item.text for item in exc_el.findall("Item") if item.text]
        lower_items = [x.strip().lower() for x in existing_items]

        needed_rules = ["\\__STORE\\", "*\\__STORE\\"]
        added = []

        for rule in needed_rules:
            if rule.lower() not in lower_items:
                item_el = ET.SubElement(exc_el, "Item")
                item_el.text = rule
                added.append(rule)

        # Ensure *.partial is in exclude
        if "*.partial" not in lower_items:
            item_el = ET.SubElement(exc_el, "Item")
            item_el.text = "*.partial"
            added.append("*.partial")

        # Also check and harden pair-specific filters
        relevant_pairs = find_relevant_folder_pairs(root)
        for p in relevant_pairs:
            if p["has_custom_filter"]:
                pf = p["filter_element"]
                p_exc_el = pf.find("Exclude")
                if p_exc_el is None:
                    p_exc_el = ET.SubElement(pf, "Exclude")
                p_existing = [item.text for item in p_exc_el.findall("Item") if item.text]
                p_lower = [x.strip().lower() for x in p_existing]
                for rule in needed_rules + ["*.partial"]:
                    if rule.lower() not in p_lower:
                        item_el = ET.SubElement(p_exc_el, "Item")
                        item_el.text = rule
                        added.append(f"pair:{rule}")

        if added:
            # Backup first
            ts = int(time.time())
            ts_backup = f"{path}.bak.{ts}"
            std_backup = f"{path}{backup_suffix}"
            shutil.copy2(path, ts_backup)
            shutil.copy2(path, std_backup)

            ET.indent(tree, space="    ")
            tmp_path = f"{path}.tmp.{ts}"
            tree.write(tmp_path, encoding="utf-8", xml_declaration=True)
            os.replace(tmp_path, path)
            modified_files.append({"path": path, "added_rules": added, "backup": ts_backup, "schema_type": schema_type})
            logger.info("Updated FFS config %s: added %s (schema: %s)", path, added, schema_type)

    # Mandatory re-audit after hardening (R7)
    post_audit = audit_ffs_configuration(batch_file, gui_file)
    overall_safe = bool(post_audit.get("overall_safe", False))
    return {
        "ok": overall_safe,
        "overall_safe": overall_safe,
        "modified": modified_files,
        "audit": post_audit,
        "error": None if overall_safe else "Post-hardening audit failed safety check"
    }


def restore_ffs_backup(batch_path=None, gui_path=None, backup_suffix=".bak"):
    """Restore FFS configurations from .bak backup."""
    batch_file = batch_path or DEFAULT_BATCH_PATH
    gui_file = gui_path or DEFAULT_GUI_PATH

    restored = []
    for path in [batch_file, gui_file]:
        bak_file = f"{path}{backup_suffix}"
        if os.path.isfile(bak_file):
            shutil.copy2(bak_file, path)
            restored.append(path)

    return {"ok": True, "restored": restored}


def generate_leakage_report(output_path=None, destinations=None):
    """Generate sanitized backup leakage report for sync dir, and detailed report for local appdata (R8)."""
    # 1. Sanitized audit for sync export (NO sample filenames, NO document titles)
    sanitized_audit = audit_plaintext_leakage_on_destinations(destinations, include_samples=False)
    cfg = audit_ffs_configuration()

    total_dest_affected = sum(1 for v in sanitized_audit.values() if v.get("store_exists"))
    total_bytes = sum(v.get("total_size_bytes", 0) for v in sanitized_audit.values() if v.get("store_exists"))
    total_files = sum(v.get("file_count", 0) for v in sanitized_audit.values() if v.get("store_exists"))
    has_leakage = total_dest_affected > 0

    # Local non-synced detailed report path
    local_appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    local_report_dir = os.path.join(local_appdata, "SAITULS", "secure-apps", "reports")
    os.makedirs(local_report_dir, exist_ok=True)
    local_report_path = os.path.join(local_report_dir, "plaintext_leakage_detailed.txt")

    # Write detailed report locally (non-synced)
    detailed_lines = [
        "======================================================================",
        "SAITULS SECURE APPS - PLAINTEXT BACKUP LEAKAGE AUDIT (LOCAL DETAILED)",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "======================================================================",
        "",
        "SAFETY POLICY: Zero automatic deletions. SAITULS audits and reports only.",
        f"FreeFileSync Configuration Hardened: {cfg.get('overall_safe')}",
        "",
        "DESTINATION AUDIT DETAILS:",
        "----------------------------------------------------------------------"
    ]
    for dest, info in sanitized_audit.items():
        detailed_lines.append(f"Destination: {dest}")
        if not info.get("accessible"):
            detailed_lines.append("  Status: Inaccessible / Drive not mounted")
        elif not info.get("store_exists"):
            detailed_lines.append("  Status: Clean (No plaintext __STORE found)")
        else:
            detailed_lines.append("  Status: EXPOSED PLAINTEXT DETECTED")
            detailed_lines.append(f"  Path: {info.get('store_path')}")
            detailed_lines.append(f"  Total Size: ~{info.get('total_size_mb')} MB ({info.get('file_count')} files)")
            detailed_lines.append(f"  Cleanup command: Remove-Item -Recurse -Force \"{info.get('store_path')}\"")
        detailed_lines.append("")
    detailed_lines.append("======================================================================")
    with open(local_report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(detailed_lines) + "\n")

    # Write sanitized summary for the sync folder (NO file names, NO sample titles)
    summary_lines = [
        "======================================================================",
        "SAITULS SECURE APPS - PLAINTEXT BACKUP LEAKAGE SUMMARY",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "======================================================================",
        "",
        "SAFETY POLICY: Zero automatic deletions. SAITULS audits and reports only.",
        f"FreeFileSync Configuration Hardened: {cfg.get('overall_safe')}",
        "",
        f"Plaintext copies detected: {'YES' if has_leakage else 'NO'}",
        f"Destinations affected: {total_dest_affected}",
        f"Total approximate MB: {round(total_bytes / (1024 * 1024), 2)}",
        f"Total approximate file count: {total_files}",
        f"Cleanup required: {'YES' if has_leakage else 'NO'}",
        "",
        f"Detailed non-synced local inventory saved to:",
        f"  {local_report_path}",
        "======================================================================"
    ]
    summary_text = "\n".join(summary_lines) + "\n"

    target_path = output_path or r"V:\___VAC\__K\__SECURE_BACKUPS\PLAINTEXT_BACKUP_SUMMARY.txt"
    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(summary_text)
    except OSError:
        pass

    return {
        "ok": True,
        "output_path": target_path,
        "local_detailed_path": local_report_path,
        "audit": sanitized_audit,
        "has_leakage": has_leakage,
        "destinations_affected": total_dest_affected
    }


def verify_ffs_cutover_gate(broker, profile_id, batch_path=None, gui_path=None, destination_dir=None):
    """Safety gate for production FreeFileSync cutover (H7).

    Cutover requires:
    1. FFS configs hardened and safe (MainConfig/Filter and pair exclusions).
    2. Published backup state is strictly CURRENT_AUTHENTICATED (never CURRENT_INTEGRITY_ONLY or STALE).
    3. Published backup payload passes cryptographic integrity verification.
    """
    import sa_backup
    from sa_broker import BACKUP_CURRENT_AUTHENTICATED, BACKUP_CURRENT_INTEGRITY_ONLY

    # 1. Audit FFS configuration
    ffs_audit = audit_ffs_configuration(batch_path=batch_path, gui_path=gui_path)
    if not ffs_audit.get("overall_safe"):
        return {
            "ok": False,
            "ready_for_cutover": False,
            "reason": "FreeFileSync configuration failed security audit (plaintext mount not safely excluded)",
            "ffs_audit": ffs_audit
        }

    # 2. Check runtime backup state from broker
    runtime = broker.runtime(profile_id)
    b_state = getattr(runtime, "backup_state", None)
    if b_state == BACKUP_CURRENT_INTEGRITY_ONLY:
        return {
            "ok": False,
            "ready_for_cutover": False,
            "reason": "Backup is CURRENT_INTEGRITY_ONLY; production cutover requires CURRENT_AUTHENTICATED with verified HMAC",
            "backup_state": b_state
        }
    if b_state != BACKUP_CURRENT_AUTHENTICATED:
        return {
            "ok": False,
            "ready_for_cutover": False,
            "reason": f"Backup state is {b_state}; production cutover requires CURRENT_AUTHENTICATED",
            "backup_state": b_state
        }

    # 3. Verify backup integrity
    try:
        dest_dir = destination_dir or getattr(runtime, "backup_destination_dir", None)
        verify_res = sa_backup.verify_backup(destination_dir=dest_dir)
        if not verify_res.get("ok"):
            return {
                "ok": False,
                "ready_for_cutover": False,
                "reason": f"Published backup verification failed: {verify_res.get('error', 'unknown error')}",
                "verify_result": verify_res
            }
    except Exception as exc:
        return {
            "ok": False,
            "ready_for_cutover": False,
            "reason": f"Published backup verification raised exception: {exc}",
            "error": str(exc)
        }

    return {
        "ok": True,
        "ready_for_cutover": True,
        "backup_state": b_state,
        "ffs_audit": ffs_audit,
        "verify_result": verify_res
    }

