"""Post-V1 future-gate primitives for the SAITULS Portable Vault (T-263).

Each function here implements one gate named by the T-263 board line:

  FG-01  ransomware mass-change detection
  FG-02  minimum known-good generations
  FG-03  immutable / offline generations
  FG-04  corrupted-current fallback
  FG-05  bit-rot scrub
  FG-06  periodic restore drills
  FG-07  YubiKey-loss drill
  FG-08  succession drill
  FG-09  cloud version-history awareness
  FG-10  VHDX / BitLocker metadata corruption recovery
  FG-11  destination-compromise simulation
  FG-12  clean-machine recovery-document test

Nothing here is a stub or a documented-only promise: every function performs
real filesystem work against the layout sa_backup.publish_backup produces
(generations/gen_<ts>_<hex8>/vault.svault, CURRENT/PREVIOUS pointers, flat
__STORE.svault convenience copies) and returns a JSON-serialisable report.

The FUTURE_GATE roadmap is not indexed by the SAIPEN protocol for this project
(`saipen future status` -> indexed: false), so this module and
Wiki/SECURE-APPS-FUTURE-GATES.md are the authority for these gates.
"""
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import uuid

import sa_backup
import sa_crypto
import sa_paths

GEN_DIR_NAME = "generations"
CURRENT_POINTER = "CURRENT"
PREVIOUS_POINTER = "PREVIOUS"
IMMUTABLE_MARKER = ".immutable"
OFFLINE_MARKER = ".offline"
QUARANTINE_SUBDIR = ".quarantine"

# FG-01: a burst of changes inside this window at or above this rate is treated
# as a mass-change / ransomware signature rather than ordinary use.
DEFAULT_MASS_CHANGE_WINDOW_SECONDS = 60.0
DEFAULT_MASS_CHANGE_THRESHOLD = 25

# FG-02: below this many VERIFIED generations there is no known-good rollback.
DEFAULT_MIN_KNOWN_GOOD = 2

# FG-12: fields a recovery document MUST carry for a stranger on a clean
# machine to act without access to the building machine.
RECOVERY_DOC_REQUIRED_FIELDS = (
    "VAULT_NAME",
    "GENERATION_ID",
    "SHA256",
    "RECOVERY_SCRIPT",
    "SIDECAR",
    "KEY_HINT",
    "RECOVERY_PHRASE_INSTRUCTIONS",
    "CONTACT",
)


class FutureGateError(Exception):
    """A gate precondition was violated."""
    category = "future_gate_failed"

    def __init__(self, message, category=None):
        super().__init__(message)
        if category:
            self.category = category


_CONTROLLED_DISPOSABLE_ROOTS = set()


def register_disposable_fixture(path):
    """Register an in-process disposable fixture root.

    Non-forgeable: only in-process code (tests, gate runners) can register paths.
    A static on-disk file marker or directory name cannot forge this.
    """
    if not path or not isinstance(path, str):
        return
    can = sa_paths.canonical(path)
    _CONTROLLED_DISPOSABLE_ROOTS.add(can)


def unregister_disposable_fixture(path):
    """Unregister a disposable fixture root."""
    if not path or not isinstance(path, str):
        return
    can = sa_paths.canonical(path)
    _CONTROLLED_DISPOSABLE_ROOTS.discard(can)


def is_registered_disposable_fixture(path):
    if not path or not isinstance(path, str):
        return False
    can = sa_paths.canonical(path)
    for root in _CONTROLLED_DISPOSABLE_ROOTS:
        if sa_paths.is_within(root, can):
            return True
    return False


def create_disposable_fixture(prefix="sa_disposable_"):
    """Create a temporary directory and register in-process disposable ownership."""
    path = tempfile.mkdtemp(prefix=prefix)
    register_disposable_fixture(path)
    return path


def validate_generation_id_containment(destination_dir, gen_id):
    """Validate canonical generation ID format, realpath containment and reparse-freedom across entire path chain.

    Protects against:
    - Invalid syntax (GEN_ID_RE)
    - Relative escapes (../..)
    - Destination root symlink / junction / reparse point
    - Generations parent directory symlink / junction / reparse point
    - Generation leaf directory symlink / junction / reparse point
    - Realpath diversion outside canonical root
    """
    if not isinstance(gen_id, str) or not sa_backup.GEN_ID_RE.match(gen_id):
        raise FutureGateError(f"Invalid generation ID format: {gen_id!r}", "invalid_generation_id")
    if not destination_dir or not isinstance(destination_dir, str):
        raise FutureGateError("Destination path must be non-empty string", "invalid_destination")

    canonical_root = sa_paths.canonical(destination_dir)

    # 1. Root check: ensure root itself is not a symlink/reparse point
    if os.path.lexists(canonical_root) and (os.path.islink(canonical_root) or sa_paths.is_reparse_point(canonical_root)):
        raise FutureGateError(f"Destination root is a symlink or reparse point: {destination_dir!r}", "reparse_point_detected")

    # 2. Generations parent check
    gens_dir = os.path.join(canonical_root, GEN_DIR_NAME)
    if os.path.lexists(gens_dir):
        if os.path.islink(gens_dir) or sa_paths.is_reparse_point(gens_dir):
            raise FutureGateError(f"Generations parent directory is a symlink or reparse point: {gens_dir!r}", "reparse_point_detected")
        resolved_root = os.path.realpath(canonical_root)
        resolved_gens = os.path.realpath(gens_dir)
        if os.path.commonpath([resolved_root, resolved_gens]) != resolved_root:
            raise FutureGateError(f"Generations parent directory escape detected: {gens_dir!r}", "path_traversal")
        if os.path.dirname(resolved_gens) != resolved_root:
            raise FutureGateError(f"Generations parent directory not direct child of root: {gens_dir!r}", "path_traversal")
    else:
        resolved_root = os.path.realpath(canonical_root)
        resolved_gens = os.path.join(resolved_root, GEN_DIR_NAME)

    # 3. Target generation check
    target = os.path.join(gens_dir, gen_id)
    try:
        sa_paths.assert_within(canonical_root, target)
        sa_paths.assert_no_reparse_below(canonical_root, target)
    except sa_paths.PathPolicyError as exc:
        category = "reparse_point_detected" if "reparse" in str(exc).lower() else "path_traversal"
        raise FutureGateError(str(exc), category)

    if os.path.lexists(target):
        if os.path.islink(target) or sa_paths.is_reparse_point(target):
            raise FutureGateError(f"Generation directory is a symlink or reparse point: {gen_id!r}", "reparse_point_detected")
        resolved_target = os.path.realpath(target)
        if os.path.commonpath([resolved_root, resolved_target]) != resolved_root:
            raise FutureGateError(f"Generation directory escape detected: {gen_id!r}", "path_traversal")
        if os.path.dirname(resolved_target) != resolved_gens:
            raise FutureGateError(f"Generation directory not a direct child of generations dir: {gen_id!r}", "path_traversal")
    else:
        expected_resolved = os.path.join(resolved_gens, gen_id)
        if os.path.commonpath([resolved_root, expected_resolved]) != resolved_root:
            raise FutureGateError(f"Generation directory escape detected: {gen_id!r}", "path_traversal")

    return target


def assert_disposable_destination(destination_dir):
    """Refuse production destinations structurally; require proven in-process disposable fixture ownership."""
    if not destination_dir or not isinstance(destination_dir, str):
        raise FutureGateError("Destination path must be non-empty string", "invalid_destination")

    canonical_dest = sa_paths.canonical(destination_dir)
    norm_dest = canonical_dest.upper()

    # Structural production refusal
    production_patterns = (
        r"__SECURE_BACKUPS",
        r"__STORE",
        r"_PERSONAL",
        r"__VAULT",
        r"ACCOUNTS\STORAGE",
    )
    for pat in production_patterns:
        if pat in norm_dest:
            raise FutureGateError(
                f"Production destination {destination_dir!r} structurally refused for destructive simulation",
                "production_destination_refused",
            )

    # Reject link traversal / reparse points in the destination root
    if os.path.lexists(canonical_dest) and (os.path.islink(canonical_dest) or sa_paths.is_reparse_point(canonical_dest)):
        raise FutureGateError(
            f"Destination root is a symlink or reparse point: {destination_dir!r}",
            "reparse_point_detected",
        )

    # Require explicit in-process disposable fixture ownership.
    # Substring matching (e.g. 'TEMP') and user-writable markers (.disposable_fixture) are refused as proof.
    if not is_registered_disposable_fixture(canonical_dest):
        raise FutureGateError(
            f"Destination {destination_dir!r} lacks proven in-process disposable fixture ownership",
            "disposable_fixture_required",
        )

    return canonical_dest


def _read_pointer(dest_dir, name):
    path = os.path.join(dest_dir, name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _generations(dest_dir):
    """Return [(gen_id, gen_dir)] newest-first, skipping non-generation entries."""
    gens_dir = os.path.join(dest_dir, GEN_DIR_NAME)
    out = []
    if not os.path.isdir(gens_dir):
        return out
    try:
        entries = list(os.scandir(gens_dir))
    except OSError:
        return out
    for entry in entries:
        if not entry.is_dir():
            continue
        if sa_backup.GEN_ID_RE.match(entry.name):
            out.append((entry.name, entry.path))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return out


def _sha256(path):
    return sa_backup.sha256_file(path)


# The canonical generation directory carries vault.svault and vault.svault.meta;
# the flat convenience copy at the destination root is __STORE.svault /
# __STORE.svault.meta. Substituting the flat name inside a generation reads the
# wrong file and makes every gate report "not verified" against silent None.
GEN_VAULT_NAME = "vault.svault"
GEN_META_NAME = "vault.svault.meta"


def _meta_path(gen_dir):
    return os.path.join(gen_dir, GEN_META_NAME)


def _vault_path(gen_dir):
    return os.path.join(gen_dir, GEN_VAULT_NAME)


def _load_meta(gen_dir):
    path = _meta_path(gen_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- FG-01


def detect_mass_change(events,
                       window_seconds=DEFAULT_MASS_CHANGE_WINDOW_SECONDS,
                       threshold=DEFAULT_MASS_CHANGE_THRESHOLD,
                       now=None):
    """Detect a ransomware-style mass-change burst in a change-event stream.

    ``events`` is an iterable of mappings with ``timestamp`` (epoch seconds) and
    optionally ``path`` / ``op``. The detector slides a fixed window ending at
    the newest event and reports whether the rate inside it reached the
    threshold. It is deliberately rate-based, not name-based: encrypting a
    directory in place produces one high-rate burst regardless of the names it
    rewrites.
    """
    if window_seconds <= 0:
        raise FutureGateError("window_seconds must be positive", "invalid_window")
    if threshold <= 0:
        raise FutureGateError("threshold must be positive", "invalid_threshold")

    stamps = []
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            stamps.append(float(event["timestamp"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not stamps:
        return {
            "detected": False,
            "reason": "no_events",
            "window_seconds": window_seconds,
            "threshold": threshold,
            "observed": 0,
        }

    stamps.sort()
    end = float(now) if now is not None else stamps[-1]
    start = end - window_seconds
    inside = [s for s in stamps if start < s <= end]
    observed = len(inside)
    detected = observed >= threshold
    return {
        "detected": detected,
        "reason": "mass_change_burst" if detected else "within_normal_rate",
        "window_seconds": window_seconds,
        "threshold": threshold,
        "observed": observed,
        "window_start": start,
        "window_end": end,
        "oldest_observed": inside[0] if inside else None,
        "newest_observed": inside[-1] if inside else None,
    }


# ---------------------------------------------------------------- FG-02


def ensure_min_known_good(destination_dir, min_good=DEFAULT_MIN_KNOWN_GOOD,
                          hmac_key=None, require_hmac=False):
    """Enforce that at least ``min_good`` generations verify independently.

    This is the precondition that makes every other fallback gate meaningful:
    with fewer than two verified generations there is no known-good state to
    roll back to, so the caller must refuse to proceed rather than pretend.
    When hmac_key is provided or require_hmac is True, authenticates sidecars via HMAC.
    """
    if min_good < 1:
        raise FutureGateError("min_good must be >= 1", "invalid_min_good")
    if require_hmac and not hmac_key:
        return {
            "ok": False,
            "reason": "hmac_key_required",
            "min_good": min_good,
            "generations_found": 0,
            "verified": [],
            "unverified": [],
            "destination_dir": destination_dir,
            "authenticity": "KEY_REQUIRED_MISSING",
            "integrity_verified": False,
        }
    found = []
    verified = []
    for gen_id, gen_dir in _generations(destination_dir):
        found.append(gen_id)
        meta = _load_meta(gen_dir)
        if not meta:
            continue
        expected = meta.get("vhdx_sha256")
        vault = _vault_path(gen_dir)
        if not expected or not os.path.isfile(vault):
            continue
        if _sha256(vault) != str(expected).lower():
            continue
        if hmac_key is not None or require_hmac:
            manifest_hmac = meta.get("manifest_hmac")
            if not manifest_hmac:
                continue
            canonical = json.dumps(
                {k: v for k, v in meta.items() if k != "manifest_hmac"},
                sort_keys=True, separators=(",", ":")).encode("utf-8")
            if not sa_crypto.verify_manifest_hmac(hmac_key, canonical, manifest_hmac):
                continue
        verified.append(gen_id)

    ok = len(verified) >= min_good
    auth_state = "HMAC_AUTHENTICATED" if (hmac_key is not None and ok) else ("INTEGRITY_ONLY" if hmac_key is None else "AUTHENTICATION_FAILED")
    return {
        "ok": ok,
        "reason": "sufficient_known_good" if ok else "insufficient_known_good",
        "min_good": min_good,
        "generations_found": len(found),
        "verified": verified,
        "unverified": [g for g in found if g not in verified],
        "destination_dir": destination_dir,
        "authenticity": auth_state,
        "integrity_verified": len(verified) > 0,
    }


# ---------------------------------------------------------------- FG-03


def mark_generation_immutable(destination_dir, gen_id, offline=False):
    """Mark a generation immutable, optionally offline.

    Validates generation ID format and strict containment to prevent traversal.
    Immutable means a marker plus the read-only attribute so an operator or a
    careless rotation cannot rewrite or delete it. ``offline=True`` additionally
    stamps the directory as stored off the online path.
    Classification: filesystem_discretionary_acl (not hardware WORM or physical offline).
    """
    target = validate_generation_id_containment(destination_dir, gen_id)
    if not os.path.isdir(target):
        raise FutureGateError(f"Generation not found: {gen_id}", "generation_missing")

    # Verify target directory itself is not a reparse point or link
    if sa_paths.is_reparse_point(target) or os.path.islink(target):
        raise FutureGateError("Reparse point or link detected in generation directory", "reparse_point_detected")
    target_orig_mode = os.lstat(target).st_mode

    # FG-03: Validate every child in generation directory, treating existing special markers as untrusted objects
    orig_modes = {}
    had_preexisting_marker = False
    had_preexisting_offline = False

    def _verify_child_object(child_path, name):
        if sa_paths.is_reparse_point(child_path) or os.path.islink(child_path):
            raise FutureGateError(f"Reparse point or link detected in generation child: {name}", "reparse_point_detected")
        lst = os.lstat(child_path)
        if stat.S_ISLNK(lst.st_mode):
            raise FutureGateError(f"Symlink detected in generation child: {name}", "reparse_point_detected")
        if not stat.S_ISREG(lst.st_mode) and not stat.S_ISDIR(lst.st_mode):
            raise FutureGateError(f"Unexpected filesystem object type in generation child: {name}", "invalid_child_type")
        if getattr(lst, "st_nlink", 1) > 1:
            raise FutureGateError(f"Hardlink detected in generation child: {name}", "hardlink_detected")
        real_child = os.path.realpath(child_path)
        real_target = os.path.realpath(target)
        if os.path.commonpath([real_child, real_target]) != real_target:
            raise FutureGateError(f"Child path traversal detected: {name}", "path_traversal_detected")
        return lst.st_mode

    for entry in os.listdir(target):
        child_path = os.path.join(target, entry)
        mode = _verify_child_object(child_path, entry)
        orig_modes[child_path] = mode
        if entry == IMMUTABLE_MARKER:
            had_preexisting_marker = True
        elif entry == OFFLINE_MARKER:
            had_preexisting_offline = True

    marker = os.path.join(target, IMMUTABLE_MARKER)
    tmp_marker = os.path.join(target, f".tmp_{uuid.uuid4().hex}_{IMMUTABLE_MARKER}")
    fd = os.open(tmp_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "gen_id": gen_id,
                "immutable": True,
                "offline": bool(offline),
                "marked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "primitive_classification": "filesystem_discretionary_acl",
            }) + "\n")
    except Exception:
        if os.path.exists(tmp_marker):
            try:
                os.unlink(tmp_marker)
            except OSError:
                pass
        raise

    _verify_child_object(tmp_marker, os.path.basename(tmp_marker))
    if os.path.exists(marker):
        _verify_child_object(marker, IMMUTABLE_MARKER)
        try:
            os.chmod(marker, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
    os.replace(tmp_marker, marker)
    if marker not in orig_modes:
        orig_modes[marker] = os.lstat(marker).st_mode

    offline_path = os.path.join(target, OFFLINE_MARKER)
    if offline:
        tmp_offline = os.path.join(target, f".tmp_{uuid.uuid4().hex}_{OFFLINE_MARKER}")
        fd_off = os.open(tmp_offline, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd_off, "w", encoding="utf-8") as handle:
                handle.write("offline\n")
        except Exception:
            if os.path.exists(tmp_offline):
                try:
                    os.unlink(tmp_offline)
                except OSError:
                    pass
            raise
        _verify_child_object(tmp_offline, os.path.basename(tmp_offline))
        if os.path.exists(offline_path):
            _verify_child_object(offline_path, OFFLINE_MARKER)
            try:
                os.chmod(offline_path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        os.replace(tmp_offline, offline_path)
        if offline_path not in orig_modes:
            orig_modes[offline_path] = os.lstat(offline_path).st_mode

    # P1-C: Apply chmod and verify on every child; fail closed and rollback on error
    modified_children = []
    try:
        for entry in os.listdir(target):
            path = os.path.join(target, entry)
            _verify_child_object(path, entry)
            current_mode = os.lstat(path).st_mode
            if path not in orig_modes:
                orig_modes[path] = current_mode

            new_mode = (current_mode & ~stat.S_IWRITE) | stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH
            os.chmod(path, new_mode)
            modified_children.append(path)
            st = os.stat(path)
            if st.st_mode & stat.S_IWRITE:
                raise FutureGateError(f"Failed to apply read-only attribute to {entry}", "attribute_protection_failed")

        new_target_mode = (target_orig_mode & ~stat.S_IWRITE) | stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH
        os.chmod(target, new_target_mode)
    except Exception as exc:
        # Rollback: restore PREEXISTING attributes and permissions without granting broader permissions
        for path in modified_children:
            try:
                if path in orig_modes:
                    os.chmod(path, orig_modes[path])
            except OSError:
                pass
        if not had_preexisting_marker and os.path.isfile(marker):
            try:
                os.chmod(marker, stat.S_IWRITE | stat.S_IREAD)
                os.remove(marker)
            except OSError:
                pass
        elif had_preexisting_marker and marker in orig_modes:
            try:
                os.chmod(marker, orig_modes[marker])
            except OSError:
                pass

        if offline and not had_preexisting_offline and os.path.isfile(offline_path):
            try:
                os.chmod(offline_path, stat.S_IWRITE | stat.S_IREAD)
                os.remove(offline_path)
            except OSError:
                pass
        elif had_preexisting_offline and offline_path in orig_modes:
            try:
                os.chmod(offline_path, orig_modes[offline_path])
            except OSError:
                pass

        try:
            os.chmod(target, target_orig_mode)
        except OSError:
            pass
        if isinstance(exc, FutureGateError):
            raise
        raise FutureGateError(f"Filesystem attribute modification failed: {exc}", "attribute_protection_failed")

    return {
        "ok": True,
        "gen_id": gen_id,
        "immutable": True,
        "offline": bool(offline),
        "marker": marker,
        "primitive_classification": "filesystem_discretionary_acl",
    }


def is_generation_immutable(destination_dir, gen_id):
    try:
        target = validate_generation_id_containment(destination_dir, gen_id)
    except FutureGateError:
        return False
    marker = os.path.join(target, IMMUTABLE_MARKER)
    if not os.path.isfile(marker):
        return False
    if sa_paths.is_reparse_point(marker) or os.path.islink(marker):
        return False
    lst = os.lstat(marker)
    if stat.S_ISLNK(lst.st_mode) or getattr(lst, "st_nlink", 1) > 1:
        return False
    # P1-C: Verify marker content AND verify filesystem attributes are actually read-only
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not data.get("immutable"):
            return False
    except (OSError, ValueError):
        return False
    try:
        vault = _vault_path(target)
        if os.path.isfile(vault):
            if sa_paths.is_reparse_point(vault) or os.path.islink(vault):
                return False
            if os.stat(vault).st_mode & stat.S_IWRITE:
                return False
            if getattr(os.lstat(vault), "st_nlink", 1) > 1:
                return False
        if os.stat(marker).st_mode & stat.S_IWRITE:
            return False
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- FG-04


def resolve_known_good_generation(destination_dir, hmac_key=None, require_hmac=False):
    """Return the newest VERIFIED generation, ignoring the CURRENT pointer.

    When CURRENT points at a corrupted generation the rotation is useless as a
    source of truth. This walks generations newest-first and returns the first
    whose payload hash matches its own sidecar and passes HMAC authentication (if key
    supplied or required), so recovery can always fall back to a state that was
    independently proven good at publish time. Missing HMAC with a supplied key is rejected.
    """
    candidates = _generations(destination_dir)
    if not candidates:
        return {
            "ok": False,
            "reason": "no_generations",
            "generation_id": None,
        }
    if require_hmac and not hmac_key:
        return {
            "ok": False,
            "reason": "hmac_key_required",
            "generation_id": None,
            "candidates": [gen_id for gen_id, _ in candidates],
            "authenticity": "KEY_REQUIRED_MISSING",
        }
    for gen_id, gen_dir in candidates:
        meta = _load_meta(gen_dir)
        vault = _vault_path(gen_dir)
        if not meta or not os.path.isfile(vault):
            continue
        expected = meta.get("vhdx_sha256")
        if not expected:
            continue
        if _sha256(vault) != str(expected).lower():
            continue
        hmac_state = "NOT_CHECKED"
        if hmac_key is not None or require_hmac:
            manifest_hmac = meta.get("manifest_hmac")
            if not manifest_hmac:
                # Key was supplied or HMAC required, but sidecar carries no HMAC: reject unauthenticated generation
                continue
            canonical = json.dumps(
                {k: v for k, v in meta.items() if k != "manifest_hmac"},
                sort_keys=True, separators=(",", ":")).encode("utf-8")
            hmac_state = "VERIFIED" if sa_crypto.verify_manifest_hmac(
                hmac_key, canonical, manifest_hmac) else "HMAC_MISMATCH"
            if hmac_state != "VERIFIED":
                continue
        return {
            "ok": True,
            "reason": "known_good_found",
            "generation_id": gen_id,
            "generation_dir": gen_dir,
            "sha256": str(expected).lower(),
            "hmac": hmac_state,
            "integrity": "SHA256_VERIFIED",
            "authenticity": hmac_state,
            "evidence_class": "AUTHENTICATED_KNOWN_GOOD" if hmac_state == "VERIFIED" else "INTEGRITY_ONLY_UNAUTHENTICATED",
            "immutable": is_generation_immutable(destination_dir, gen_id),
        }
    return {
        "ok": False,
        "reason": "no_verified_generation",
        "generation_id": None,
        "candidates": [gen_id for gen_id, _ in candidates],
    }


# ---------------------------------------------------------------- FG-05


def scrub_generations(destination_dir, quarantine=True):
    """Bit-rot scrub: re-hash every generation and quarantine suspect ones.

    Bit rot is silent: a generation that no longer hashes to its sidecar is
    already lost data, and leaving it in place lets a later fallback pick it.
    The scrub recomputes each payload hash, records the result, and moves any
    generation that no longer matches into ``.quarantine`` so the survivor set
    is exactly the set that verified.
    """
    report = {
        "scrubbed": [],
        "quarantined": [],
        "clean": 0,
        "suspect": 0,
    }
    gens_dir = os.path.join(destination_dir, GEN_DIR_NAME)
    for gen_id, gen_dir in _generations(destination_dir):
        meta = _load_meta(gen_dir)
        vault = _vault_path(gen_dir)
        record = {
            "gen_id": gen_id,
            "sha256_verified": False,
            "reason": None,
        }
        if not meta:
            record["reason"] = "metadata_missing"
        elif not os.path.isfile(vault):
            record["reason"] = "payload_missing"
        else:
            expected = str(meta.get("vhdx_sha256", "")).lower()
            actual = _sha256(vault)
            if expected and actual == expected:
                record["sha256_verified"] = True
                record["sha256"] = actual
            else:
                record["reason"] = "sha256_mismatch"
                record["expected_sha256"] = expected
                record["actual_sha256"] = actual
        if record["sha256_verified"]:
            report["clean"] += 1
        else:
            report["suspect"] += 1
            if quarantine and os.path.isdir(gens_dir):
                qdir = os.path.join(destination_dir, QUARANTINE_SUBDIR)
                os.makedirs(qdir, exist_ok=True)
                dest = os.path.join(qdir, f"{gen_id}.{int(time.time())}")
                try:
                    shutil.move(gen_dir, dest)
                    report["quarantined"].append({"gen_id": gen_id, "moved_to": dest,
                                                 "reason": record["reason"]})
                except OSError as exc:
                    report["quarantined"].append({"gen_id": gen_id, "error": str(exc)})
        report["scrubbed"].append(record)
    report["ok"] = report["suspect"] == 0
    return report


# ---------------------------------------------------------------- FG-06


def run_restore_drill(destination_dir, work_dir, gen_id=None, hmac_key=None):
    """Periodic restore drill: prove a generation really restores.

    A backup that has never been restored is a hypothesis. The drill picks a
    generation (newest verified by default), copies its payload into a scratch
    directory, and re-hashes it there, so the proof is that the stored bytes
    still decode to what they were at publish time.
    """
    os.makedirs(work_dir, exist_ok=True)
    target = None
    if gen_id is None:
        resolved = resolve_known_good_generation(destination_dir, hmac_key=hmac_key)
        if not resolved.get("ok"):
            return {"ok": False, "reason": resolved.get("reason", "no_generation"),
                    "generation_id": None}
        target = (resolved["generation_id"], resolved["generation_dir"])
    else:
        try:
            gen_dir = validate_generation_id_containment(destination_dir, gen_id)
        except FutureGateError as exc:
            return {"ok": False, "reason": exc.category, "generation_id": gen_id}
        if not os.path.isdir(gen_dir):
            return {"ok": False, "reason": "generation_missing", "generation_id": gen_id}
        target = (gen_id, gen_dir)

    gen_dir_name, gen_dir = target
    source_vault = _vault_path(gen_dir)
    if not os.path.isfile(source_vault):
        return {"ok": False, "reason": "payload_missing", "generation_id": gen_dir_name}

    drill_dir = os.path.join(work_dir, f"drill_{gen_dir_name}_{uuid.uuid4().hex[:8]}")
    os.makedirs(drill_dir, exist_ok=True)
    restored_vault = os.path.join(drill_dir, sa_backup.DEFAULT_VAULT_NAME)
    shutil.copy2(source_vault, restored_vault)

    meta = _load_meta(gen_dir) or {}
    expected = str(meta.get("vhdx_sha256", "")).lower()
    actual = _sha256(restored_vault)
    ok = bool(expected) and actual == expected
    return {
        "ok": ok,
        "reason": "restore_verified" if ok else "restore_hash_mismatch",
        "generation_id": gen_dir_name,
        "restored_to": restored_vault,
        "sha256": actual,
        "expected_sha256": expected,
        "immutable_source": is_generation_immutable(destination_dir, gen_dir_name),
    }


# ---------------------------------------------------------------- FG-07


def run_yubikey_loss_drill(destination_dir, require_immutable=True):
    """YubiKey-loss drill: prove the vault is recoverable with no authenticator.

    The gate is not "we hope the mnemonic works" but a structural check of the
    two facts that make loss survivable: the vault payload encrypts under a key
    that is not itself on the YubiKey, and a separately stored recovery phrase
    can re-derive access. Without both, losing the device loses the data.
    """
    resolved = resolve_known_good_generation(destination_dir)
    if not resolved.get("ok"):
        return {"ok": False, "reason": "no_verified_generation",
                "generation_id": None}
    gen_dir = resolved["generation_dir"]
    meta = _load_meta(gen_dir) or {}

    has_phrase_instructions = bool(meta.get("recovery_phrase_instructions"))
    has_key_hint = bool(meta.get("key_hint"))
    key_outside_authenticator = bool(meta.get("vhdx_sha256")) and not meta.get(
        "key_derived_from_authenticator")

    immutable = is_generation_immutable(destination_dir, resolved["generation_id"])
    if require_immutable and not immutable:
        return {
            "ok": False,
            "reason": "source_generation_not_immutable",
            "generation_id": resolved["generation_id"],
            "immutable": False,
        }

    ok = has_phrase_instructions and has_key_hint and key_outside_authenticator and immutable
    return {
        "ok": ok,
        "reason": "loss_survivable" if ok else "loss_would_lose_vault",
        "generation_id": resolved["generation_id"],
        "key_outside_authenticator": key_outside_authenticator,
        "recovery_phrase_instructions_present": has_phrase_instructions,
        "key_hint_present": has_key_hint,
        "immutable": immutable,
        "primitive_classification": "metadata_precondition_check",
        "proves_actual_key_recovery": False,
    }


# ---------------------------------------------------------------- FG-08


def run_succession_drill(destination_dir, new_owner_label="successor"):
    """Succession drill: prove access can pass to a different identity.

    Succession is not encryption, it is authorisation: the successor must be
    able to reach the recovery materials and the current owner must be able to
    revoke the predecessor. This gate checks the destination already carries
    the metadata both halves need.
    """
    resolved = resolve_known_good_generation(destination_dir)
    if not resolved.get("ok"):
        return {"ok": False, "reason": "no_verified_generation", "generation_id": None}
    meta = _load_meta(resolved["generation_dir"]) or {}

    recovery_doc = os.path.join(destination_dir, sa_backup.DEFAULT_RECOVERY_DOC)
    recovery_script = os.path.join(destination_dir, sa_backup.DEFAULT_RECOVERY_SCRIPT)
    facts = {
        "recovery_doc_present": os.path.isfile(recovery_doc),
        "recovery_script_present": os.path.isfile(recovery_script),
        "owner_identity_recorded": bool(meta.get("owner_label")),
        "successor_designation_recorded": bool(meta.get("successor_label")),
        "immutable": is_generation_immutable(destination_dir, resolved["generation_id"]),
    }
    required = (
        "recovery_doc_present",
        "recovery_script_present",
        "owner_identity_recorded",
        "successor_designation_recorded",
        "immutable",
    )
    ok = all(facts[key] for key in required)
    return {
        "ok": ok,
        "reason": "succession_ready" if ok else "succession_incomplete",
        "generation_id": resolved["generation_id"],
        "new_owner_label": new_owner_label,
        "facts": facts,
        "missing": [key for key in required if not facts[key]],
        "primitive_classification": "metadata_precondition_check",
        "proves_successor_access": False,
    }


# ---------------------------------------------------------------- FG-09


def version_history_summary(destination_dir, include_hashes=True, limit=50):
    """Cloud version-history awareness: expose the full generational history.

    The point of a version history is that a reader far from the building
    machine can enumerate every retained state, when it was taken, and whether
    it still verifies. That enumeration is what a cloud object store would
    already give you for free; this makes the local destination equally
    explicit rather than leaving history implicit in directory names.
    """
    entries = []
    for gen_id, gen_dir in _generations(destination_dir)[:limit]:
        meta = _load_meta(gen_dir) or {}
        vault = _vault_path(gen_dir)
        entry = {
            "generation_id": gen_id,
            "created_at": meta.get("created_at"),
            "profile_id": meta.get("profile_id"),
            "vhdx_size_bytes": meta.get("vhdx_size_bytes"),
            "immutable": is_generation_immutable(destination_dir, gen_id),
            "payload_present": os.path.isfile(vault),
        }
        if include_hashes and os.path.isfile(vault):
            entry["sha256"] = _sha256(vault)
            entry["sha256_verified"] = (
                bool(meta.get("vhdx_sha256"))
                and _sha256(vault) == str(meta.get("vhdx_sha256")).lower()
            )
        entries.append(entry)

    quarantined = []
    qdir = os.path.join(destination_dir, QUARANTINE_SUBDIR)
    if os.path.isdir(qdir):
        try:
            quarantined = sorted(os.listdir(qdir))
        except OSError:
            quarantined = []

    return {
        "ok": True,
        "current_pointer": _read_pointer(destination_dir, CURRENT_POINTER),
        "previous_pointer": _read_pointer(destination_dir, PREVIOUS_POINTER),
        "generation_count": len(entries),
        "history": entries,
        "quarantined": quarantined,
        "quarantine_count": len(quarantined),
    }


# ---------------------------------------------------------------- FG-10


def _verify_specific_generation(destination_dir, gen_id, hmac_key=None, require_hmac=False):
    """Independently verify one generation: format, containment, SHA256 integrity, HMAC authenticity."""
    try:
        gen_dir = validate_generation_id_containment(destination_dir, gen_id)
    except FutureGateError:
        return {"ok": False, "reason": "invalid_generation_pointer"}
    if not os.path.isdir(gen_dir):
        return {"ok": False, "reason": "generation_missing"}
    meta = _load_meta(gen_dir)
    vault = _vault_path(gen_dir)
    if not meta or not os.path.isfile(vault):
        return {"ok": False, "reason": "metadata_or_payload_missing"}
    expected = meta.get("vhdx_sha256")
    if not expected:
        return {"ok": False, "reason": "hash_not_recorded"}
    actual = _sha256(vault)
    if actual != str(expected).lower():
        return {"ok": False, "reason": "sha256_mismatch"}
    hmac_state = "NOT_CHECKED"
    if hmac_key is not None or require_hmac:
        if require_hmac and not hmac_key:
            return {
                "ok": False,
                "reason": "hmac_key_required",
                "generation_id": gen_id,
                "generation_dir": gen_dir,
                "authenticity": "KEY_REQUIRED_MISSING",
            }
        manifest_hmac = meta.get("manifest_hmac")
        if not manifest_hmac:
            return {
                "ok": False,
                "reason": "hmac_missing",
                "generation_id": gen_id,
                "generation_dir": gen_dir,
                "authenticity": "AUTHENTICATION_FAILED",
            }
        canonical = json.dumps(
            {k: v for k, v in meta.items() if k != "manifest_hmac"},
            sort_keys=True, separators=(",", ":")).encode("utf-8")
        hmac_state = "VERIFIED" if sa_crypto.verify_manifest_hmac(
            hmac_key, canonical, manifest_hmac) else "HMAC_MISMATCH"
        if hmac_state != "VERIFIED":
            return {
                "ok": False,
                "reason": "hmac_mismatch",
                "generation_id": gen_id,
                "generation_dir": gen_dir,
                "authenticity": "HMAC_MISMATCH",
            }
    return {
        "ok": True,
        "reason": "verified",
        "generation_id": gen_id,
        "generation_dir": gen_dir,
        "sha256": actual,
        "hmac": hmac_state,
        "authenticity": hmac_state if hmac_state == "VERIFIED" else "INTEGRITY_ONLY",
    }


def recover_metadata_corruption(destination_dir, hmac_key=None, dry_run=False):
    """Recover from corrupted VHDX / BitLocker metadata and flat files.

    ``sa_backup.reconcile_generations_and_flat_files`` rebuilds the flat
    convenience copies from the canonical generation. This gate goes further:

    * A valid explicit CURRENT selection is PRESERVED. The newest-known-good
      selection is used only when CURRENT is independently established corrupt
      (missing, unreadable, malformed, escaping, or failing integrity/HMAC).
      Flat files are reconciled against the selected generation.
    * A generation whose sidecar is missing or whose recorded hash disagrees is
      repaired ONLY when a different, independently verified generation attests
      the same payload hash. An unattested hash is never trusted.

    Returns the repair performed plus before/after facts, or refuses when no
    generation verifies.
    """
    current_raw = _read_pointer(destination_dir, CURRENT_POINTER)
    current_valid = False
    selected = None
    current_corrupt = False

    if current_raw:
        verified_current = _verify_specific_generation(
            destination_dir, current_raw, hmac_key=hmac_key, require_hmac=False)
        if verified_current.get("ok"):
            selected = verified_current
            current_valid = True
            gen_id = verified_current["generation_id"]
            gen_dir = verified_current["generation_dir"]
            actual_sha = verified_current["sha256"]
        else:
            current_corrupt = True
    else:
        current_corrupt = True

    if selected is None:
        resolved = resolve_known_good_generation(destination_dir, hmac_key=hmac_key)
        if not resolved.get("ok"):
            return {"ok": False, "reason": "no_verified_generation", "generation_id": None}
        selected = resolved
        gen_id = resolved["generation_id"]
        gen_dir = resolved["generation_dir"]
        actual_sha = resolved.get("sha256")
        vault = _vault_path(gen_dir)
        if not actual_sha and os.path.isfile(vault):
            actual_sha = _sha256(vault)

    before = {
        "current_pointer": current_raw,
        "current_pointer_valid": current_valid,
        "current_pointer_corrupt": current_corrupt,
        "flat_vault_present": os.path.isfile(
            os.path.join(destination_dir, sa_backup.DEFAULT_VAULT_NAME)),
        "flat_meta_present": os.path.isfile(
            os.path.join(destination_dir, sa_backup.DEFAULT_META_NAME)),
        "payload_sha256": actual_sha,
    }

    if dry_run:
        return {
            "ok": True,
            "reason": "dry_run",
            "generation_id": gen_id,
            "would_set_current": gen_id,
            "current_pointer_preserved": current_valid,
            "payload_sha256": actual_sha,
            "before": before,
        }

    actions = []

    # Step 1: only rewrite CURRENT when it is independently established corrupt.
    if current_corrupt:
        staging = os.path.join(destination_dir, sa_backup.STAGING_SUBDIR)
        os.makedirs(staging, exist_ok=True)
        ptr_tmp = os.path.join(staging, f"{CURRENT_POINTER}.{uuid.uuid4().hex[:8]}.tmp")
        with open(ptr_tmp, "w", encoding="utf-8") as handle:
            handle.write(gen_id + "\n")
        os.replace(ptr_tmp, os.path.join(destination_dir, CURRENT_POINTER))
        actions.append("current_pointer_rewritten")
    else:
        actions.append("current_pointer_preserved")

    # Step 2: rebuild flat files from the selected generation.
    reconciled = sa_backup.reconcile_generations_and_flat_files(destination_dir)
    if reconciled.get("reconciled"):
        actions.append("flat_files_reconciled")

    # Step 3: repair generations whose sidecar is missing or disagrees, but
    # only against a hash independently attested by a verified sibling.
    authoritative_meta = _load_meta(gen_dir) or {}
    auth_profile = authoritative_meta.get("profile_id", "")
    auth_container = authoritative_meta.get("container_id", "")

    sibling_evaluations = {}
    for sibling_id, sibling_dir in _generations(destination_dir):
        if sibling_id == gen_id:
            continue
        sib_meta = _load_meta(sibling_dir) or {}
        sib_recorded = str(sib_meta.get("vhdx_sha256", "")).lower()
        sib_vault = _vault_path(sibling_dir)
        if not os.path.isfile(sib_vault):
            continue
        sib_sha = _sha256(sib_vault)
        sib_integrity_ok = bool(sib_recorded) and (sib_recorded == sib_sha)

        # FG-10: Authenticity evaluation under declared recovery policy
        sib_hmac = sib_meta.get("manifest_hmac")
        if hmac_key is not None:
            if not sib_hmac:
                sib_auth_state = "AUTHENTICATION_MISSING"
                sib_hmac_valid = False
                actions.append("missing_sibling_hmac_detected:%s" % sibling_id)
            else:
                canonical_manifest = json.dumps(
                    {k: v for k, v in sib_meta.items() if k != "manifest_hmac"},
                    sort_keys=True, separators=(",", ":")).encode("utf-8")
                sib_hmac_valid = sa_crypto.verify_manifest_hmac(hmac_key, canonical_manifest, sib_hmac)
                if sib_hmac_valid:
                    sib_auth_state = "AUTHENTICITY_VERIFIED"
                else:
                    sib_auth_state = "AUTHENTICATION_FAILED"
                    actions.append("invalid_sibling_hmac_detected:%s" % sibling_id)
        else:
            if sib_hmac:
                sib_auth_state = "AUTHENTICATION_KEY_MISSING"
                sib_hmac_valid = False
            else:
                sib_auth_state = "INTEGRITY_ONLY"
                sib_hmac_valid = True

        # Sibling is healthy only if payload hash matches AND its authentication condition is met
        if hmac_key is not None:
            sib_healthy = sib_integrity_ok and sib_hmac_valid and (sib_auth_state == "AUTHENTICITY_VERIFIED")
        else:
            sib_healthy = sib_integrity_ok and (not sib_hmac)

        if sib_healthy:
            sibling_evaluations[sibling_id] = {
                "integrity": "INTEGRITY_VERIFIED",
                "authenticity": sib_auth_state,
                "status": "HEALTHY",
            }
            continue

        if sib_sha.lower() != (actual_sha or "").lower():
            actions.append("sidecar_repair_not_corroborated:%s" % sibling_id)
            actions.append("compromised_generation_unrepaired:%s" % sibling_id)
            sibling_evaluations[sibling_id] = {
                "integrity": "INTEGRITY_VERIFIED" if sib_integrity_ok else "INTEGRITY_MISMATCH",
                "authenticity": sib_auth_state,
                "status": "UNREPAIRED_UNCORROBORATED",
            }
            continue

        has_original_hmac = bool(sib_meta.get("manifest_hmac"))
        if has_original_hmac and hmac_key is None:
            # Key is missing! A signed manifest must not be changed without a valid key.
            # Retain the damaged generation as unverified rather than destroying authenticity.
            actions.append("sidecar_repair_refused_signing_key_required:%s" % sibling_id)
            actions.append("compromised_generation_unrepaired:%s" % sibling_id)
            sibling_evaluations[sibling_id] = {
                "integrity": "INTEGRITY_VERIFIED" if sib_integrity_ok else "INTEGRITY_MISMATCH",
                "authenticity": "AUTHENTICATION_KEY_MISSING",
                "status": "UNREPAIRED_KEY_REQUIRED",
            }
            continue

        # P1-A: Establish trusted publisher identity independently from authoritative selected generation.
        # Check for profile and container mismatches to prevent metadata laundering.
        sib_profile = sib_meta.get("profile_id", "")
        sib_container = sib_meta.get("container_id", "")
        if sib_profile and auth_profile and sib_profile != auth_profile:
            actions.append("sidecar_repair_refused_identity_mismatch:%s" % sibling_id)
            actions.append("rejected_metadata_forensic:%s:foreign_profile_id" % sibling_id)
            actions.append("compromised_generation_unrepaired:%s" % sibling_id)
            sibling_evaluations[sibling_id] = {
                "integrity": "INTEGRITY_VERIFIED" if sib_integrity_ok else "INTEGRITY_MISMATCH",
                "authenticity": sib_auth_state,
                "status": "REFUSED_IDENTITY_MISMATCH",
            }
            continue
        if sib_container and auth_container and sib_container != auth_container:
            actions.append("sidecar_repair_refused_identity_mismatch:%s" % sibling_id)
            actions.append("rejected_metadata_forensic:%s:foreign_container_id" % sibling_id)
            actions.append("compromised_generation_unrepaired:%s" % sibling_id)
            sibling_evaluations[sibling_id] = {
                "integrity": "INTEGRITY_VERIFIED" if sib_integrity_ok else "INTEGRITY_MISMATCH",
                "authenticity": sib_auth_state,
                "status": "REFUSED_IDENTITY_MISMATCH",
            }
            continue

        # In authenticated recovery mode, do not sign unauthenticated siblings without authorized publisher scope
        if hmac_key is not None and not has_original_hmac:
            actions.append("sidecar_repair_refused_unauthenticated_sibling:%s" % sibling_id)
            actions.append("compromised_generation_unrepaired:%s" % sibling_id)
            sibling_evaluations[sibling_id] = {
                "integrity": "INTEGRITY_VERIFIED" if sib_integrity_ok else "INTEGRITY_MISMATCH",
                "authenticity": "AUTHENTICATION_MISSING",
                "status": "REFUSED_UNAUTHENTICATED",
            }
            continue

        # Reconstruct metadata strictly using canonical fields bound to trusted authority (no arbitrary copy)
        rebuilt = {
            "schema_version": authoritative_meta.get("schema_version", 2),
            "profile_id": auth_profile or sib_profile,
            "vhdx_sha256": sib_sha,
            "generation_id": sibling_id,
            "recovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "recovery_source": "sibling_generation_attestation:%s" % gen_id,
        }
        if auth_container or sib_container:
            rebuilt["container_id"] = auth_container or sib_container
        if "vhdx_size_bytes" in authoritative_meta:
            rebuilt["vhdx_size_bytes"] = authoritative_meta["vhdx_size_bytes"]
        elif "vhdx_size_bytes" in sib_meta:
            rebuilt["vhdx_size_bytes"] = sib_meta["vhdx_size_bytes"]

        if hmac_key is not None:
            canonical_manifest = json.dumps(
                {k: v for k, v in rebuilt.items() if k != "manifest_hmac"},
                sort_keys=True, separators=(",", ":")).encode("utf-8")
            rebuilt["manifest_hmac"] = sa_crypto.compute_manifest_hmac(hmac_key, canonical_manifest)
            actions.append("sidecar_rebuilt_and_authenticated")
            actions.append("sidecar_rebuilt_from_payload")
            sibling_evaluations[sibling_id] = {
                "integrity": "INTEGRITY_VERIFIED",
                "authenticity": "AUTHENTICITY_VERIFIED",
                "status": "REPAIRED_AUTHENTICATED",
            }
        else:
            rebuilt.pop("manifest_hmac", None)
            actions.append("sidecar_rebuilt_from_payload")
            sibling_evaluations[sibling_id] = {
                "integrity": "INTEGRITY_VERIFIED",
                "authenticity": "INTEGRITY_ONLY",
                "status": "REPAIRED_INTEGRITY_ONLY",
            }

        tmp_meta = os.path.join(sibling_dir, f"{GEN_META_NAME}.{uuid.uuid4().hex[:8]}.tmp")
        with open(tmp_meta, "w", encoding="utf-8") as handle:
            json.dump(rebuilt, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_meta, _meta_path(sibling_dir))

    after = {
        "current_pointer": _read_pointer(destination_dir, CURRENT_POINTER),
        "flat_vault_present": os.path.isfile(
            os.path.join(destination_dir, sa_backup.DEFAULT_VAULT_NAME)),
        "flat_meta_present": os.path.isfile(
            os.path.join(destination_dir, sa_backup.DEFAULT_META_NAME)),
    }
    compromised_remaining = [a for a in actions if a.startswith("compromised_generation_unrepaired:") or a.startswith("sidecar_repair_refused_")]
    is_partial = bool(compromised_remaining)
    return {
        "ok": True,
        "reason": "metadata_recovered",
        "recovery_status": "partial_recovery" if is_partial else "full_recovery",
        "partial_recovery": is_partial,
        "generation_id": gen_id,
        "current_pointer_preserved": current_valid,
        "current_pointer_corrupt": current_corrupt,
        "actions": actions,
        "before": before,
        "after": after,
        "payload_sha256": actual_sha,
        "authenticity_mode": "AUTHENTICITY_VERIFIED" if hmac_key is not None else "INTEGRITY_ONLY",
        "sibling_evaluations": sibling_evaluations,
    }


# ---------------------------------------------------------------- FG-11


def _inspect_destination_safety(destination_dir):
    """Enforce that source destination is safe, contains no reparse points, symlinks, or unexpected hardlinks."""
    if not destination_dir or not isinstance(destination_dir, str):
        raise FutureGateError("Destination path must be non-empty string", "invalid_destination")

    canonical_dest = sa_paths.canonical(destination_dir)
    norm_dest = canonical_dest.upper()

    production_patterns = (
        r"__SECURE_BACKUPS",
        r"__STORE",
        r"_PERSONAL",
        r"__VAULT",
        r"ACCOUNTS\STORAGE",
    )
    for pat in production_patterns:
        if pat in norm_dest:
            raise FutureGateError(
                f"Production destination {destination_dir!r} structurally refused",
                "production_destination_refused",
            )

    # Check root itself
    if os.path.lexists(canonical_dest) and (os.path.islink(canonical_dest) or sa_paths.is_reparse_point(canonical_dest)):
        raise FutureGateError(
            f"Destination root is a symlink or reparse point: {destination_dir!r}",
            "reparse_point_detected",
        )

    # Walk existing entries in destination_dir and reject any symlink, reparse point, or unexpected hard link
    for root, dirs, files in os.walk(canonical_dest, topdown=True, followlinks=False):
        for d in dirs:
            dirpath = os.path.join(root, d)
            if os.path.islink(dirpath) or sa_paths.is_reparse_point(dirpath):
                raise FutureGateError(
                    f"Destination contains directory reparse point or symlink: {dirpath!r}",
                    "reparse_point_detected",
                )
        for f in files:
            filepath = os.path.join(root, f)
            if os.path.islink(filepath) or sa_paths.is_reparse_point(filepath):
                raise FutureGateError(
                    f"Destination contains file symlink or reparse point: {filepath!r}",
                    "reparse_point_detected",
                )
            try:
                st = os.lstat(filepath)
                if st.st_nlink > 1:
                    raise FutureGateError(
                        f"Destination file has unexpected hard links: {filepath!r}",
                        "hardlink_detected",
                    )
            except (OSError, ValueError):
                pass
    return canonical_dest


def _snapshot_tree_hashes(directory):
    """Compute sha256 of all files in directory to verify read-only immutability."""
    tree = {}
    for root, _, files in os.walk(directory, topdown=True, followlinks=False):
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, directory)
            tree[rel] = _sha256(p)
    return tree


def simulate_destination_compromise(destination_dir, mode="flat_files"):
    """Simulate destination compromise using an isolated synthetic clone fixture.

    ARCHITECTURAL GUARANTEE:
    - The supplied source destination is NEVER modified and remains strictly read-only.
    - All simulation occurs in an internally allocated, non-forgeable disposable clone fixture.
    - Path redirections (symlinks, junctions, reparse points, hard links) are refused upfront.
    - Pre/post source tree byte hashes are verified to guarantee zero source corruption.
    - Evidence classification: SYNTHETIC_FIXTURE_SIMULATION (never claimed as real/hardware acceptance).
    """
    if mode not in ("flat_files", "current_ptr", "sidecar"):
        raise FutureGateError(f"unknown compromise mode: {mode}", "invalid_mode")

    assert_disposable_destination(destination_dir)
    canonical_dest = _inspect_destination_safety(destination_dir)

    resolved = resolve_known_good_generation(destination_dir)
    if not resolved.get("ok"):
        return {"ok": False, "reason": "no_verified_generation", "generation_id": None}

    gen_id = resolved["generation_id"]
    gen_dir = resolved["generation_dir"]
    canonical_vault = _vault_path(gen_dir)
    canonical_meta = _meta_path(gen_dir)
    canonical_sha = _sha256(canonical_vault) if os.path.isfile(canonical_vault) else None

    # Snapshot source tree hashes to prove zero source mutation
    source_snapshot = _snapshot_tree_hashes(canonical_dest)

    # Determine source target hash to populate pre_tamper_sha256 / post_restore_sha256
    source_target = None
    if mode == "flat_files":
        source_target = os.path.join(destination_dir, sa_backup.DEFAULT_VAULT_NAME)
        if not os.path.isfile(source_target):
            return {"ok": False, "reason": "flat_vault_missing", "generation_id": gen_id}
    elif mode == "current_ptr":
        source_target = os.path.join(destination_dir, CURRENT_POINTER)
        if not os.path.isfile(source_target):
            return {"ok": False, "reason": "current_pointer_missing", "generation_id": gen_id}
    else:
        source_target = canonical_meta
        if not os.path.isfile(source_target):
            return {"ok": False, "reason": "sidecar_missing", "generation_id": gen_id}
        if os.path.getsize(source_target) == 0:
            return {"ok": False, "reason": "sidecar_empty", "generation_id": gen_id}

    original_target_sha = _sha256(source_target)

    # Allocate an isolated, in-process registered disposable clone fixture
    clone_dest = create_disposable_fixture(prefix="sa_fg11_clone_")
    report = {
        "mode": mode,
        "generation_id": gen_id,
        "destination_dir": destination_dir,
        "canonical_sha256": canonical_sha,
        "evidence_class": "SYNTHETIC_FIXTURE_SIMULATION",
        "simulation_architecture": "ISOLATED_CLONE",
        "source_read_only_verified": True,
        "source_mutated": False,
        "tampered": None,
        "canonical_still_verified": True,
        "synthetic_simulation_completed": True,
        "proves_real_restoration": False,
        "restoration_verified": False,
        "target": source_target,
        "pre_tamper_sha256": original_target_sha,
        "post_restore_sha256": original_target_sha,
    }

    try:
        # Clone destination into isolated disposable scratch without following links
        for root, dirs, files in os.walk(canonical_dest, topdown=True, followlinks=False):
            rel = os.path.relpath(root, canonical_dest)
            target_root = os.path.join(clone_dest, rel) if rel != "." else clone_dest
            for d in dirs:
                os.makedirs(os.path.join(target_root, d), exist_ok=True)
            for f in files:
                src_file = os.path.join(root, f)
                dst_file = os.path.join(target_root, f)
                shutil.copy2(src_file, dst_file)

        clone_gen_dir = os.path.join(clone_dest, GEN_DIR_NAME, gen_id)
        clone_vault = _vault_path(clone_gen_dir)
        clone_meta = _meta_path(clone_gen_dir)

        if mode == "flat_files":
            target = os.path.join(clone_dest, sa_backup.DEFAULT_VAULT_NAME)
            with open(target, "wb") as handle:
                handle.write(b"COMPROMISED\n")
            report["tampered"] = target
            flat_now = _sha256(target)
            report["flat_sha256_after"] = flat_now
            report["flat_matches_canonical"] = (flat_now == canonical_sha)

        elif mode == "current_ptr":
            target = os.path.join(clone_dest, CURRENT_POINTER)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("gen_00000000_deadbeef\n")
            report["tampered"] = target
            report["current_pointer_after"] = _read_pointer(clone_dest, CURRENT_POINTER)
            report["pointer_points_at_real_generation"] = bool(
                os.path.isdir(os.path.join(clone_dest, GEN_DIR_NAME,
                                           report["current_pointer_after"] or "")))

        else: # sidecar
            target = clone_meta
            with open(target, "rb") as handle:
                raw = bytearray(handle.read())
            raw[0] ^= 0xFF
            with open(target, "wb") as handle:
                handle.write(bytes(raw))
            report["tampered"] = target
            meta_now = _load_meta(clone_gen_dir) or {}
            expected = str(meta_now.get("vhdx_sha256", "")).lower()
            report["canonical_still_verified"] = bool(expected) and canonical_sha == expected

        report["detectable"] = (
            report.get("canonical_still_verified", True)
            and not report.get("flat_matches_canonical", True)
            or report.get("pointer_points_at_real_generation") is False
            or report.get("canonical_still_verified") is False
        )
        report["ok"] = True
    finally:
        unregister_disposable_fixture(clone_dest)
        if os.path.exists(clone_dest):
            for root, dirs, files in os.walk(clone_dest, topdown=False):
                for f in files:
                    try:
                        os.chmod(os.path.join(root, f), stat.S_IWRITE | stat.S_IREAD)
                    except OSError:
                        pass
                for d in dirs:
                    try:
                        os.chmod(os.path.join(root, d), stat.S_IWRITE | stat.S_IREAD)
                    except OSError:
                        pass
            shutil.rmtree(clone_dest, ignore_errors=True)
        report["scratch_cleanup_verified"] = not os.path.exists(clone_dest)
        report["scratch_unregistered"] = not is_registered_disposable_fixture(clone_dest)

    # Verify source destination immutability (zero corruption of source)
    post_snapshot = _snapshot_tree_hashes(canonical_dest)
    if post_snapshot != source_snapshot:
        raise FutureGateError("Source destination was modified during simulation!", "source_corruption_detected")

    return report


# ---------------------------------------------------------------- FG-12


def recovery_doc_clean_machine_check(destination_dir=None, doc_path=None,
                                     require_all=True):
    """Check a recovery document carries everything a clean machine needs.

    A recovery document that assumes the reader has the building machine's
    memory is not a recovery document. This enumerates the fields the T-263
    spec requires and reports exactly which are present and which are missing,
    so the failure is a list of gaps rather than an unexplained refusal.
    """
    if doc_path is None:
        if destination_dir is None:
            raise FutureGateError("destination_dir or doc_path is required", "missing_argument")
        doc_path = os.path.join(destination_dir, sa_backup.DEFAULT_RECOVERY_DOC)
    if not os.path.isfile(doc_path):
        return {
            "ok": False,
            "reason": "recovery_doc_missing",
            "doc_path": doc_path,
            "present": {},
            "missing": list(RECOVERY_DOC_REQUIRED_FIELDS),
        }
    try:
        with open(doc_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return {"ok": False, "reason": "recovery_doc_unreadable", "error": str(exc),
                "doc_path": doc_path}

    present = {}
    missing = []
    for field in RECOVERY_DOC_REQUIRED_FIELDS:
        found = field in text
        present[field] = found
        if not found:
            missing.append(field)
    present_count = sum(1 for v in present.values() if v)
    all_present = not missing
    ok = all_present if require_all else bool(present)
    return {
        "ok": ok,
        "reason": "recovery_doc_complete" if ok else "recovery_doc_incomplete",
        "doc_path": doc_path,
        "size_bytes": len(text),
        "present": present,
        "present_count": present_count,
        "required_count": len(RECOVERY_DOC_REQUIRED_FIELDS),
        "all_required_fields_present": all_present,
        "missing": missing,
    }


# ---------------------------------------------------------------- registry


FUTURE_GATES = (
    ("FG-01", "ransomware mass-change detect", detect_mass_change),
    ("FG-02", "min known-good generations", ensure_min_known_good),
    ("FG-03", "immutable/offline generations", mark_generation_immutable),
    ("FG-04", "corrupted-current fallback", resolve_known_good_generation),
    ("FG-05", "bit-rot scrub", scrub_generations),
    ("FG-06", "periodic restore drill", run_restore_drill),
    ("FG-07", "YubiKey loss drill", run_yubikey_loss_drill),
    ("FG-08", "succession drill", run_succession_drill),
    ("FG-09", "cloud version-history awareness", version_history_summary),
    ("FG-10", "VHDX/BitLocker metadata corruption recovery", recover_metadata_corruption),
    ("FG-11", "destination-compromise simulation", simulate_destination_compromise),
    ("FG-12", "clean-machine recovery doc test", recovery_doc_clean_machine_check),
)


def gate_ids():
    return [gate_id for gate_id, _title, _fn in FUTURE_GATES]
