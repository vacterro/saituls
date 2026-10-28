"""Portable Encrypted Vault Backup and Recovery for SAITULS Secure Apps.

Architecture:
- The canonical vault format is BitLocker-encrypted VHDX (.svault).
- Backups are bitwise copies/snapshots of the detached, encrypted VHDX.
- FreeFileSync (FFS) replicates these encrypted VHDX copies without seeing plaintext.
- atomic publishing: disk space check -> copy to .staging/<tx>.partial ->
  SHA-256 match -> rotate to __STORE.previous.svault & .meta -> atomic rename to __STORE.svault.
- Sidecar metadata: __STORE.svault.meta & __STORE.previous.svault.meta (public metadata,
  truthful authentication, safe portable FIDO enrollment envelopes).
- Windows-native emergency recovery: __STORE-RECOVERY.txt & __STORE-RECOVER.ps1.
"""
import re
import copy
import hashlib
import json
import logging
import ntpath
import os
import shutil
import time
import uuid

import sa_auth
import sa_crypto
import sa_layout
import sa_paths
import sa_storage

logger = logging.getLogger("saituls.secure_apps.backup")

DEFAULT_BACKUP_DIR = r"V:\___VAC\__K\__SECURE_BACKUPS"
DEFAULT_VAULT_NAME = "__STORE.svault"
DEFAULT_PREVIOUS_NAME = "__STORE.previous.svault"
DEFAULT_META_NAME = "__STORE.svault.meta"
DEFAULT_PREVIOUS_META_NAME = "__STORE.previous.svault.meta"
DEFAULT_RECOVERY_DOC = "__STORE-RECOVERY.txt"
DEFAULT_RECOVERY_SCRIPT = "__STORE-RECOVER.ps1"

STAGING_SUBDIR = ".staging"
MIN_FREE_SPACE_RATIO = 1.5

GEN_ID_RE = re.compile(r"^gen_[0-9]+_[0-9a-f]{8}$")


def validate_generation_id(gen_id, generations_dir):
    """Validate that gen_id matches canonical syntax and is strictly contained in generations_dir (H10)."""
    if not isinstance(gen_id, str) or not GEN_ID_RE.match(gen_id):
        raise BackupError(f"Invalid generation ID format: {gen_id!r}", "INVALID_GENERATION_ID")
    target_dir = os.path.join(generations_dir, gen_id)
    resolved_target = os.path.realpath(target_dir)
    resolved_parent = os.path.realpath(generations_dir)
    if os.path.commonpath([resolved_parent, resolved_target]) != resolved_parent:
        raise BackupError(f"Generation directory escape detected: {gen_id!r}", "PATH_ESCAPE_DETECTED")
    if os.path.dirname(resolved_target) != resolved_parent:
        raise BackupError(f"Generation directory not a direct child of generations dir: {gen_id!r}", "PATH_ESCAPE_DETECTED")
    return resolved_target


class BackupError(Exception):
    """Base error for backup operations."""
    category = "backup_failed"

    def __init__(self, message, category=None):
        super().__init__(message)
        if category:
            self.category = category


class BackupCancelled(BackupError):
    """Backup was cancelled by user or coordinator."""
    category = "backup_cancelled"


def sha256_file(path, chunk_size=16 * 1024 * 1024):
    """Compute SHA-256 hex digest of a file in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest().lower()


def copy_file_chunked(src, dst, chunk_size=16 * 1024 * 1024, cancel_token=None):
    """Copy a file in chunks while calculating sha256 and checking for cancellation."""
    h = hashlib.sha256()
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            if cancel_token and cancel_token.is_set():
                raise BackupCancelled("backup copy cancelled", "backup_cancelled")
            chunk = fsrc.read(chunk_size)
            if not chunk:
                break
            fdst.write(chunk)
            h.update(chunk)
    return h.hexdigest().lower()


def clean_stale_staging(staging_dir, max_age_seconds=86400):
    """Remove any stale .partial/.tmp files and gen_* staging directories older than max_age_seconds (H11)."""
    if not os.path.isdir(staging_dir):
        return
    resolved_staging = os.path.realpath(staging_dir)
    now = time.time()
    try:
        for entry in os.scandir(staging_dir):
            try:
                # Reparse point / junction defense (H11)
                is_reparse = False
                try:
                    st = entry.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    if (attrs & 0x0400) or entry.is_symlink():  # FILE_ATTRIBUTE_REPARSE_POINT = 0x400
                        is_reparse = True
                except OSError:
                    is_reparse = True

                resolved_entry = os.path.realpath(entry.path)
                # Strict direct child containment
                if os.path.dirname(resolved_entry) != resolved_staging:
                    continue
                if os.path.commonpath([resolved_staging, resolved_entry]) != resolved_staging:
                    continue

                if is_reparse:
                    # Refuse to traverse into reparse points; remove the link itself if stale
                    age = now - entry.stat(follow_symlinks=False).st_mtime
                    if age > max_age_seconds:
                        if entry.is_dir(follow_symlinks=False):
                            os.rmdir(entry.path)
                        else:
                            os.unlink(entry.path)
                    continue

                age = now - entry.stat().st_mtime
                if age > max_age_seconds:
                    if entry.is_dir() and (GEN_ID_RE.match(entry.name) or entry.name.endswith(".tmp")):
                        shutil.rmtree(entry.path, ignore_errors=True)
                    elif entry.is_file() and (entry.name.endswith(".partial") or entry.name.endswith(".tmp") or entry.name.endswith(".meta.tmp")):
                        os.remove(entry.path)
            except OSError:
                pass
    except OSError:
        pass


def generate_recovery_doc(vault_name=DEFAULT_VAULT_NAME):
    """Generate self-contained human-readable recovery guide (R9)."""
    return f"""================================================================================
SAITULS SECURE APPS — ENCRYPTED VAULT EMERGENCY RECOVERY
================================================================================

Vault File: {vault_name}
Format: Microsoft BitLocker XTS-AES-256 Encrypted VHDX (NTFS)

This vault is fully self-contained and portable. It can be opened on ANY
Windows 10 Pro / Enterprise or Windows 11 Pro / Enterprise machine
WITHOUT SAITULS, Python, or any third-party software installed.

--------------------------------------------------------------------------------
METHOD 1: Windows Explorer (Standard GUI)
--------------------------------------------------------------------------------
1. Copy or rename '{vault_name}' to have a '.vhdx' file extension:
   e.g. Copy-Item '{vault_name}' 'recovery_vault.vhdx'
2. Double-click the .vhdx file. Windows native VHDX driver mounts it as a virtual drive.
3. In File Explorer, locate the new drive marked with a BitLocker lock icon.
4. Click the locked drive:
   - Enter your Portable Master Password when prompted.
   - OR select 'More options' -> 'Enter recovery key' and type the 48-digit key
     (e.g. 123456-789012-345678-...).
5. The drive unlocks and files are immediately accessible.
6. When finished: Right-click the drive letter in File Explorer and select 'Eject'.

--------------------------------------------------------------------------------
METHOD 2: Standalone PowerShell Recovery Script
--------------------------------------------------------------------------------
Right-click '{DEFAULT_RECOVERY_SCRIPT}' and select 'Run with PowerShell'
(Run as Administrator).
The script automatically mounts the image, prompts for password/key, unlocks
the drive, opens Explorer, and cleanly dismounts when you are finished.

--------------------------------------------------------------------------------
METHOD 3: Windows PowerShell Command Line (Manual)
--------------------------------------------------------------------------------
Open PowerShell as Administrator:

1. Ensure the file has a .vhdx extension (copy or rename if needed):
   Copy-Item "{vault_name}" "recovery_vault.vhdx"

2. Mount the VHDX:
   $img = Mount-DiskImage -ImagePath "recovery_vault.vhdx" -NoDriveLetter -PassThru
   $vol = (Get-Disk -Number $img.Number | Get-Partition | Get-Volume).DriveLetter

3. Unlock using Portable Master Password:
   Unlock-BitLocker -MountPoint "$($vol):" -Password (Read-Host -AsSecureString "Enter Password")

   OR Unlock using 48-digit Recovery Key:
   Unlock-BitLocker -MountPoint "$($vol):" -RecoveryPassword "123456-..."

4. Dismount when finished:
   Lock-BitLocker -MountPoint "$($vol):"
   Dismount-DiskImage -ImagePath "recovery_vault.vhdx"
================================================================================
"""


def generate_recovery_script(vault_name=DEFAULT_VAULT_NAME):
    """Generate hardened standalone PowerShell recovery script requiring zero dependencies."""
    return f"""<#
.SYNOPSIS
    Standalone Native Windows Recovery Script for SAITULS Encrypted Vault.
    Requires NO SAITULS installation, NO Python, NO third-party tools.
#>
param(
    [string]$VaultPath = (Join-Path $PSScriptRoot "{vault_name}"),
    [switch]$AllowWrite,
    [switch]$ReadOnly
)

$ErrorActionPreference = "Stop"

$fullPath = [System.IO.Path]::GetFullPath($VaultPath)

# By default, recovery attaches read-only to avoid corrupting or altering the backup (Phase 4).
$isReadOnly = ($ReadOnly.IsPresent -or (-not $AllowWrite.IsPresent))

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host "[!] Elevating to Administrator..." -ForegroundColor Yellow
    $argsList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -VaultPath `"$fullPath`""
    if ($AllowWrite) {{ $argsList += " -AllowWrite" }}
    if ($ReadOnly) {{ $argsList += " -ReadOnly" }}
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argsList
    exit
}}

if (-not (Test-Path -LiteralPath $fullPath)) {{
    Write-Error "Vault file not found: $fullPath"
    exit 1
}}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " SAITULS SECURE VAULT - WINDOWS NATIVE RECOVERY" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Vault: $fullPath"

# Sidecar metadata hash check (H14)
$metaPath = "$fullPath.meta"
if (-not (Test-Path -LiteralPath $metaPath)) {{
    $parentDir = Split-Path $fullPath
    $candidate = Join-Path $parentDir "__STORE.svault.meta"
    if (Test-Path -LiteralPath $candidate) {{ $metaPath = $candidate }}
}}

if (Test-Path -LiteralPath $metaPath) {{
    try {{
        $metaContent = Get-Content -LiteralPath $metaPath -Raw | ConvertFrom-Json
        $expectedSha = $metaContent.vhdx_sha256
        if ($expectedSha) {{
            Write-Host "[*] Verifying payload SHA-256 against sidecar metadata..."
            $actualSha = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLower()
            if ($actualSha -eq $expectedSha.ToLower()) {{
                Write-Host "[+] Payload integrity: VERIFIED" -ForegroundColor Green
                Write-Host "[*] Sidecar authenticity: NOT VERIFIED WITHOUT YUBIKEY/SAITULS" -ForegroundColor Yellow
            }} else {{
                Write-Host ""
                Write-Host "============================================================" -ForegroundColor Red
                Write-Host " CRITICAL ERROR: PAYLOAD SHA-256 MISMATCH DETECTED!" -ForegroundColor Red
                Write-Host " Expected: $expectedSha" -ForegroundColor Red
                Write-Host " Actual:   $actualSha" -ForegroundColor Red
                Write-Host " The backup may be corrupted or tampered with." -ForegroundColor Red
                Write-Host "============================================================" -ForegroundColor Red
                $override = Read-Host "Type 'OVERRIDE' to proceed anyway at your own risk"
                if ($override -ne 'OVERRIDE') {{
                    Write-Error "Aborted due to payload hash mismatch."
                    exit 1
                }}
            }}
        }}
    }} catch {{
        Write-Warning "Could not parse sidecar metadata ($metaPath): $_"
    }}
}}

$attachPath = $fullPath
$tempCreated = $null
if ($fullPath -notmatch '\\.(vhdx|vhd)$') {{
    $tempVhdx = "$fullPath.mount.vhdx"
    $linked = $false
    try {{
        $null = cmd /c mklink /H "$tempVhdx" "$fullPath" 2>&1
        if (Test-Path -LiteralPath $tempVhdx) {{
            $attachPath = $tempVhdx
            $tempCreated = $tempVhdx
            $linked = $true
        }}
    }} catch {{ }}

    if (-not $linked) {{
        $tempDir = Join-Path $env:TEMP ("svault_recovery_" + [System.Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
        $tempCopy = Join-Path $tempDir "vault.vhdx"
        Write-Host "[*] Hard link unavailable, copying to temporary VHDX: $tempCopy ..."
        Copy-Item -LiteralPath $fullPath -Destination $tempCopy -Force
        $attachPath = $tempCopy
        $tempCreated = $tempDir
    }}
}}

$mountParams = @{{ ImagePath = $attachPath; PassThru = $true }}
if ($isReadOnly) {{
    $mountParams.Access = 'ReadOnly'
    Write-Host "[*] Attaching disk image in READ-ONLY mode (default safe inspection)..." -ForegroundColor Cyan
}} else {{
    Write-Host "[!] WARNING: Attaching disk image in WRITABLE mode (-AllowWrite specified)." -ForegroundColor Yellow
    Write-Host "    Volume metadata modifications will alter payload SHA-256 hashes." -ForegroundColor Yellow
}}

$attached = $false
try {{
    Write-Host "[*] Attaching disk image..."
    $img = Mount-DiskImage @mountParams
    $attached = $true
    $disk = Get-Disk -Number $img.Number
    $part = $disk | Get-Partition | Where-Object {{ $_.Type -ne 'Reserved' }} | Select-Object -First 1
    if (-not $part) {{ throw "No data partition found on disk $($disk.Number)" }}

    $vol = $part | Get-Volume
    $mountPoint = $null
    if ($vol -and $vol.DriveLetter) {{
        $mountPoint = "$($vol.DriveLetter):"
    }} else {{
        $used = Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Name
        $available = 68..90 | ForEach-Object {{ [char]$_ }} | Where-Object {{ $used -notcontains $_ }} | Select-Object -Last 1
        if ($available) {{
            Set-Partition -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber -NewDriveLetter $available | Out-Null
            $mountPoint = "$($available):"
        }} else {{
            throw "Unable to assign an available drive letter"
        }}
    }}

    Write-Host "[*] Volume detected at $mountPoint"
    $bl = Get-BitLockerVolume -MountPoint $mountPoint
    if ("$($bl.LockStatus)" -eq 'Locked') {{
        Write-Host ""
        Write-Host "Choose unlock method:" -ForegroundColor Yellow
        Write-Host " [1] Portable Master Password"
        Write-Host " [2] 48-Digit BitLocker Recovery Key"
        $choice = Read-Host "Choice (1 or 2)"
        if ($choice -eq '2') {{
            $recKey = Read-Host "Enter 48-digit Recovery Key (e.g. 123456-789012-...)"
            Unlock-BitLocker -MountPoint $mountPoint -RecoveryPassword $recKey.Trim() | Out-Null
        }} else {{
            $secPw = Read-Host "Enter Portable Master Password" -AsSecureString
            Unlock-BitLocker -MountPoint $mountPoint -Password $secPw | Out-Null
        }}
        $blAfter = Get-BitLockerVolume -MountPoint $mountPoint
        if ("$($blAfter.LockStatus)" -ne 'Unlocked') {{
            throw "Failed to unlock volume. Lock status remains: $($blAfter.LockStatus)"
        }}
        Write-Host "[+] Volume successfully unlocked!" -ForegroundColor Green
    }} else {{
        Write-Host "[+] Volume is already unlocked." -ForegroundColor Green
    }}

    Write-Host "[+] Plaintext vault available at: $mountPoint" -ForegroundColor Green
    Start-Process explorer.exe $mountPoint
    Write-Host "Press ENTER when finished to safely dismount and lock the vault..."
    Read-Host
}} finally {{
    if ($attached) {{
        Write-Host "[*] Dismounting image..."
        if ($mountPoint) {{
            try {{ Lock-BitLocker -MountPoint $mountPoint -ErrorAction SilentlyContinue | Out-Null }} catch {{ }}
        }}
        try {{ Dismount-DiskImage -ImagePath $attachPath | Out-Null }} catch {{ }}
        Start-Sleep -Milliseconds 500
        $chk = Get-DiskImage -ImagePath $attachPath -ErrorAction SilentlyContinue
        if ($chk -and $chk.Attached) {{
            Write-Host ""
            Write-Host "============================================================" -ForegroundColor Red
            Write-Host " WARNING: VAULT IS STILL ATTACHED!" -ForegroundColor Red
            Write-Host " CLOSE OPEN EXPLORER WINDOWS / OPEN FILES AND RETRY DETACH." -ForegroundColor Red
            Write-Host "============================================================" -ForegroundColor Red
            exit 1
        }} else {{
            Write-Host "[+] Vault safely locked and detached." -ForegroundColor Green
        }}
    }}
    if ($tempCreated -and (Test-Path -LiteralPath $tempCreated)) {{
        try {{ Remove-Item -LiteralPath $tempCreated -Recurse -Force -ErrorAction SilentlyContinue }} catch {{ }}
    }}
}}
"""


class EphemeralCredentialStore(sa_auth.CredentialStore):
    """In-memory CredentialStore holding public enrollment envelopes without touching disk."""

    def __init__(self, profile_id, container_id, enrollment_dicts):
        self.path = ":memory:"
        self._document = {
            "schema": sa_auth.CREDENTIALS_SCHEMA,
            "schema_version": sa_auth.CREDENTIALS_SCHEMA_VERSION,
            "profiles": {
                profile_id: {
                    "container_id": container_id,
                    "enrollments": copy.deepcopy(enrollment_dicts) if enrollment_dicts else []
                }
            }
        }

    def load(self):
        pass

    def save(self):
        pass

    def _commit(self, candidate):
        self._document = candidate

    def _write(self, document):
        pass


def publish_backup(profile, backend, destination_dir=None, hmac_key=None,
                   cred_store=None, force=False, cancel_token=None):
    """Publish an atomic, verified encrypted backup with dual generation rotation.

    Safety Precondition:
    The container MUST be in DETACHED or MISSING state.
    Copying a live mounted BitLocker volume can corrupt NTFS snapshot integrity.
    """
    source_path = profile.container
    if not os.path.isfile(source_path):
        raise BackupError(f"Source container does not exist: {source_path}", "source_missing")

    # Safety state check: volume must be detached
    current_state = backend.state(profile)
    if current_state not in (sa_storage.DETACHED, sa_storage.MISSING):
        raise BackupError(
            f"Cannot publish backup while vault is {current_state}. Vault must be detached first.",
            "vault_active"
        )

    dest_dir = destination_dir or DEFAULT_BACKUP_DIR
    os.makedirs(dest_dir, exist_ok=True)
    staging_dir = os.path.join(dest_dir, STAGING_SUBDIR)
    os.makedirs(staging_dir, exist_ok=True)
    clean_stale_staging(staging_dir)

    source_size = os.path.getsize(source_path)
    required_space = int(source_size * MIN_FREE_SPACE_RATIO)
    dest_free = shutil.disk_usage(dest_dir).free
    if dest_free < required_space:
        raise BackupError(
            f"Insufficient disk space in {dest_dir}. Required: {required_space // (1024*1024)} MB, "
            f"Available: {dest_free // (1024*1024)} MB",
            "insufficient_disk_space"
        )

    t0 = time.time()

    # Discover protectors and verify layout against native BitLocker list (R11)
    raw_protectors = []
    try:
        prot_res = backend.list_protectors(profile)
        if isinstance(prot_res, dict):
            raw_protectors = prot_res.get("protectors", [])
    except Exception as exc:
        logger.warning("Could not query protectors for %s: %s", profile.id, exc)

    # Export portable public enrollment envelopes (P1-1)
    portable_enrollments = []
    if cred_store is not None:
        try:
            portable_enrollments = [e.to_dict() for e in cred_store.enrollments(profile.id)]
        except Exception as exc:
            logger.warning("Could not export enrollments for %s: %s", profile.id, exc)

    layout = sa_layout.load_layout(profile)
    daily_protector_info = {}
    portable_protector_info = {}
    break_glass_protector_info = {}

    if layout is not None:
        try:
            verified_roles = sa_layout.verify_layout_against_native(layout, raw_protectors)
        except sa_layout.LayoutError as exc:
            raise BackupError(f"Protector layout drift detected: {exc}", "PROTECTOR_LAYOUT_DRIFT")

        daily_p = layout.get("daily_protector") or {}
        port_p = layout.get("portable_protector") or {}
        bg_p = layout.get("break_glass_protector") or {}

        daily_protector_info = {
            "id": daily_p.get("id"),
            "type": daily_p.get("type"),
            "present": verified_roles.get("daily_present", False),
        }
        portable_protector_info = {
            "id": port_p.get("id") if port_p else None,
            "type": port_p.get("type") if port_p else None,
            "present": verified_roles.get("portable_present", False),
        }
        break_glass_protector_info = {
            "id": bg_p.get("id"),
            "type": bg_p.get("type"),
            "present": verified_roles.get("break_glass_present", False),
        }
        protectors_summary = {
            "yubikey_fido2": bool(portable_enrollments),
            "portable_password": verified_roles.get("portable_present", False),
            "recovery_key": verified_roles.get("break_glass_present", False),
        }
    else:
        # P0-F: On V2 vaults, missing layout must fail with PROTECTOR_LAYOUT_UNKNOWN
        raw_recs = [p for p in raw_protectors if str(p.get("type", "")).lower() == "recoverypassword"]
        is_v2 = getattr(profile, "storage_layout_version", 2) == 2 or len(raw_recs) >= 2
        if is_v2:
            raise BackupError(f"Protector layout record missing or unknown for V2 vault {profile.id}", "PROTECTOR_LAYOUT_UNKNOWN")

        has_pw = any(str(p.get("type", "")).lower() == "password" for p in raw_protectors)
        has_rec = any(str(p.get("type", "")).lower() == "recoverypassword" for p in raw_protectors)
        daily_protector_info = {"present": bool(portable_enrollments)}
        portable_protector_info = {"present": has_pw}
        break_glass_protector_info = {"present": has_rec}
        protectors_summary = {
            "yubikey_fido2": bool(portable_enrollments),
            "portable_password": has_pw,
            "recovery_key": has_rec,
        }

    # Immutable generation publication (R5)
    gen_id = f"gen_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    staged_gen_dir = os.path.join(staging_dir, gen_id)
    os.makedirs(staged_gen_dir, exist_ok=True)
    staged_vault_path = os.path.join(staged_gen_dir, "vault.svault")
    staged_meta_path = os.path.join(staged_gen_dir, "vault.svault.meta")

    generations_dir = os.path.join(dest_dir, "generations")
    os.makedirs(generations_dir, exist_ok=True)
    committed_gen_dir = os.path.join(generations_dir, gen_id)

    try:
        # Step 1: Copy payload to staged generation directory while computing hash
        staged_sha256 = copy_file_chunked(source_path, staged_vault_path, cancel_token=cancel_token)

        # Step 2: Build sidecar metadata in staging
        manifest = {
            "schema_version": 2,
            "profile_id": profile.id,
            "container_id": getattr(profile, "container_id", ""),
            "storage_layout_version": 2,
            "backup_timestamp": time.time(),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "vhdx_size_bytes": source_size,
            "vhdx_sha256": staged_sha256,
            "protectors": protectors_summary,
            "daily_protector": daily_protector_info,
            "portable_protector": portable_protector_info,
            "break_glass_protector": break_glass_protector_info,
            "raw_protectors": raw_protectors,
            "portable_enrollments": portable_enrollments,
        }
        if hmac_key:
            canonical_b = json.dumps({k: v for k, v in manifest.items() if k != "manifest_hmac"},
                                     sort_keys=True, separators=(',', ':')).encode("utf-8")
            manifest["manifest_hmac"] = sa_crypto.compute_manifest_hmac(hmac_key, canonical_b)

        with open(staged_meta_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        # Step 3: Verify staged payload against staged metadata before committing
        staged_verify_sha = sha256_file(staged_vault_path)
        if staged_verify_sha.lower() != staged_sha256.lower():
            raise BackupError(f"Staged verification failed: {staged_verify_sha} != {staged_sha256}", "staged_verify_failed")

        # Step 4: Atomically rename staged generation directory into committed generations
        os.replace(staged_gen_dir, committed_gen_dir)

        # Step 5: Atomically update CURRENT and PREVIOUS pointer files
        current_ptr_file = os.path.join(dest_dir, "CURRENT")
        previous_ptr_file = os.path.join(dest_dir, "PREVIOUS")

        old_current_gen = None
        if os.path.isfile(current_ptr_file):
            try:
                with open(current_ptr_file, "r", encoding="utf-8") as f:
                    cand = f.read().strip()
                    validate_generation_id(cand, generations_dir)
                    old_current_gen = cand
            except (OSError, BackupError):
                old_current_gen = None

        tmp_cur_ptr = os.path.join(staging_dir, f"CURRENT.{uuid.uuid4().hex[:8]}.tmp")
        with open(tmp_cur_ptr, "w", encoding="utf-8") as f:
            f.write(gen_id + "\n")
        os.replace(tmp_cur_ptr, current_ptr_file)

        if old_current_gen and old_current_gen != gen_id:
            try:
                validate_generation_id(old_current_gen, generations_dir)
                tmp_prev_ptr = os.path.join(staging_dir, f"PREVIOUS.{uuid.uuid4().hex[:8]}.tmp")
                with open(tmp_prev_ptr, "w", encoding="utf-8") as f:
                    f.write(old_current_gen + "\n")
                os.replace(tmp_prev_ptr, previous_ptr_file)
            except BackupError:
                pass

        # Step 6: Maintain flat convenience files (__STORE.svault & __STORE.previous.svault)
        target_vault_path = os.path.join(dest_dir, DEFAULT_VAULT_NAME)
        previous_vault_path = os.path.join(dest_dir, DEFAULT_PREVIOUS_NAME)
        target_meta_path = os.path.join(dest_dir, DEFAULT_META_NAME)
        previous_meta_path = os.path.join(dest_dir, DEFAULT_PREVIOUS_META_NAME)

        # Rotate existing target to previous
        if os.path.exists(target_vault_path):
            if os.path.exists(previous_vault_path):
                try:
                    os.remove(previous_vault_path)
                except OSError:
                    pass
            try:
                os.replace(target_vault_path, previous_vault_path)
            except OSError:
                pass

        if os.path.exists(target_meta_path):
            if os.path.exists(previous_meta_path):
                try:
                    os.remove(previous_meta_path)
                except OSError:
                    pass
            try:
                os.replace(target_meta_path, previous_meta_path)
            except OSError:
                pass

        # Publish new convenience flat files via atomic copy/replace from committed generation
        staged_flat_vault = os.path.join(staging_dir, f"{DEFAULT_VAULT_NAME}.{uuid.uuid4().hex[:8]}.tmp")
        staged_flat_meta = os.path.join(staging_dir, f"{DEFAULT_META_NAME}.{uuid.uuid4().hex[:8]}.tmp")

        shutil.copy2(os.path.join(committed_gen_dir, "vault.svault"), staged_flat_vault)
        shutil.copy2(os.path.join(committed_gen_dir, "vault.svault.meta"), staged_flat_meta)

        os.replace(staged_flat_vault, target_vault_path)
        os.replace(staged_flat_meta, target_meta_path)

    except Exception:
        if os.path.exists(staged_gen_dir):
            try:
                shutil.rmtree(staged_gen_dir, ignore_errors=True)
            except OSError:
                pass
        raise

    # Write documentation & recovery script
    doc_path = os.path.join(dest_dir, DEFAULT_RECOVERY_DOC)
    script_path = os.path.join(dest_dir, DEFAULT_RECOVERY_SCRIPT)

    with open(doc_path + f".{gen_id}.tmp", "w", encoding="utf-8") as f:
        f.write(generate_recovery_doc(DEFAULT_VAULT_NAME))
    os.replace(doc_path + f".{gen_id}.tmp", doc_path)

    with open(script_path + f".{gen_id}.tmp", "w", encoding="utf-8") as f:
        f.write(generate_recovery_script(DEFAULT_VAULT_NAME))
    os.replace(script_path + f".{gen_id}.tmp", script_path)

    duration = time.time() - t0
    logger.info("Encrypted backup published successfully in %.2fs: %s (%s)",
                duration, target_vault_path, staged_sha256)

    return {
        "ok": True,
        "vault_path": target_vault_path,
        "previous_path": previous_vault_path if os.path.exists(previous_vault_path) else None,
        "meta_path": target_meta_path,
        "previous_meta_path": previous_meta_path if os.path.exists(previous_meta_path) else None,
        "generation_id": gen_id,
        "generations_dir": generations_dir,
        "committed_gen_dir": committed_gen_dir,
        "doc_path": doc_path,
        "script_path": script_path,
        "size_bytes": source_size,
        "sha256": staged_sha256,
        "duration_seconds": duration,
        "protectors": protectors_summary,
    }


def verify_backup(destination_dir=None, vault_name=DEFAULT_VAULT_NAME, meta_name=None, hmac_key=None):
    """Verify integrity of the published encrypted backup and its metadata (P1-2, P1-4)."""
    dest_dir = destination_dir or DEFAULT_BACKUP_DIR
    vault_path = os.path.join(dest_dir, vault_name)
    if meta_name is None:
        if vault_name == DEFAULT_PREVIOUS_NAME:
            meta_name = DEFAULT_PREVIOUS_META_NAME
        else:
            meta_name = DEFAULT_META_NAME
    meta_path = os.path.join(dest_dir, meta_name)

    if not os.path.isfile(vault_path):
        return {
            "ok": False,
            "error": f"Vault file missing: {vault_path}",
            "payload_sha256_verified": False,
            "sidecar_authenticated": "NO_METADATA",
        }
    if not os.path.isfile(meta_path):
        return {
            "ok": False,
            "error": f"Metadata sidecar missing: {meta_path}",
            "payload_sha256_verified": False,
            "sidecar_authenticated": "NO_METADATA",
        }

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Malformed metadata JSON: {exc}",
            "payload_sha256_verified": False,
            "sidecar_authenticated": "MALFORMED",
        }

    expected_sha = manifest.get("vhdx_sha256")
    if not expected_sha:
        return {
            "ok": False,
            "error": "Metadata does not contain vhdx_sha256",
            "payload_sha256_verified": False,
            "sidecar_authenticated": "MALFORMED",
        }

    actual_sha = sha256_file(vault_path)
    payload_verified = (actual_sha.lower() == expected_sha.lower())
    if not payload_verified:
        return {
            "ok": False,
            "error": f"SHA-256 mismatch: manifest {expected_sha} != actual {actual_sha}",
            "payload_sha256_verified": False,
            "sidecar_authenticated": "UNVERIFIED",
            "manifest_sha256": expected_sha,
            "actual_sha256": actual_sha,
            "vault_path": vault_path,
            "meta_path": meta_path,
        }

    # Truthful sidecar HMAC verification (P1-2)
    manifest_hmac = manifest.get("manifest_hmac")
    if manifest_hmac:
        if hmac_key is not None:
            canonical_b = json.dumps({k: v for k, v in manifest.items() if k != "manifest_hmac"},
                                     sort_keys=True, separators=(',', ':')).encode("utf-8")
            if sa_crypto.verify_manifest_hmac(hmac_key, canonical_b, manifest_hmac):
                sidecar_authenticated = "VERIFIED"
                hmac_valid = True
            else:
                sidecar_authenticated = "HMAC_MISMATCH"
                hmac_valid = False
                return {
                    "ok": False,
                    "error": "Metadata HMAC validation failed",
                    "payload_sha256_verified": True,
                    "sidecar_authenticated": "HMAC_MISMATCH",
                    "hmac_valid": False,
                    "vault_path": vault_path,
                    "meta_path": meta_path,
                }
        else:
            sidecar_authenticated = "UNVERIFIED"
            hmac_valid = None
    else:
        sidecar_authenticated = "NO_HMAC"
        hmac_valid = None

    return {
        "ok": True,
        "payload_sha256_verified": True,
        "sidecar_authenticated": sidecar_authenticated,
        "vault_path": vault_path,
        "meta_path": meta_path,
        "sha256": actual_sha,
        "actual_sha256": actual_sha,
        "size_bytes": os.path.getsize(vault_path),
        "manifest": manifest,
        "hmac_valid": hmac_valid,
    }


def reconcile_generations_and_flat_files(destination_dir=None):
    """Reconcile flat convenience files (__STORE.svault) using CURRENT generation as canonical truth (P1-G, H9, H10)."""
    dest_dir = destination_dir or DEFAULT_BACKUP_DIR
    current_ptr = os.path.join(dest_dir, "CURRENT")
    if not os.path.isfile(current_ptr):
        return {"reconciled": False, "reason": "no_current_pointer"}

    try:
        with open(current_ptr, "r", encoding="utf-8") as f:
            gen_id = f.read().strip()
    except OSError:
        return {"reconciled": False, "reason": "current_pointer_unreadable"}

    generations_dir = os.path.join(dest_dir, "generations")
    try:
        gen_dir = validate_generation_id(gen_id, generations_dir)
    except BackupError as exc:
        return {"reconciled": False, "reason": f"invalid_generation_pointer: {exc}"}

    canonical_vault = os.path.join(gen_dir, "vault.svault")
    canonical_meta = os.path.join(gen_dir, "vault.svault.meta")
    if not (os.path.isfile(canonical_vault) and os.path.isfile(canonical_meta)):
        return {"reconciled": False, "reason": "canonical_generation_missing"}

    flat_vault = os.path.join(dest_dir, DEFAULT_VAULT_NAME)
    flat_meta = os.path.join(dest_dir, DEFAULT_META_NAME)

    need_reconcile = False
    if not os.path.isfile(flat_vault) or not os.path.isfile(flat_meta):
        need_reconcile = True
    else:
        # H9: Compare actual SHA-256 and metadata content, not merely file sizes
        if sha256_file(flat_vault) != sha256_file(canonical_vault):
            need_reconcile = True
        else:
            try:
                with open(flat_meta, "rb") as f1, open(canonical_meta, "rb") as f2:
                    if f1.read() != f2.read():
                        need_reconcile = True
            except OSError:
                need_reconcile = True

    if need_reconcile:
        staging_dir = os.path.join(dest_dir, STAGING_SUBDIR)
        os.makedirs(staging_dir, exist_ok=True)
        tmp_v = os.path.join(staging_dir, f"{DEFAULT_VAULT_NAME}.reconcile.{uuid.uuid4().hex[:8]}.tmp")
        tmp_m = os.path.join(staging_dir, f"{DEFAULT_META_NAME}.reconcile.{uuid.uuid4().hex[:8]}.tmp")
        shutil.copy2(canonical_vault, tmp_v)
        shutil.copy2(canonical_meta, tmp_m)
        os.replace(tmp_v, flat_vault)
        os.replace(tmp_m, flat_meta)
        return {"reconciled": True, "generation_id": gen_id}

    return {"reconciled": False, "reason": "already_synchronized", "generation_id": gen_id}


def verify_all_generations(destination_dir=None, hmac_key=None):
    """Verify both current and previous generations independently (P1-4)."""
    dest_dir = destination_dir or DEFAULT_BACKUP_DIR
    current_res = verify_backup(dest_dir, DEFAULT_VAULT_NAME, DEFAULT_META_NAME, hmac_key=hmac_key)
    previous_res = None
    prev_vault_path = os.path.join(dest_dir, DEFAULT_PREVIOUS_NAME)
    if os.path.exists(prev_vault_path):
        previous_res = verify_backup(dest_dir, DEFAULT_PREVIOUS_NAME, DEFAULT_PREVIOUS_META_NAME, hmac_key=hmac_key)
    all_ok = current_res.get("ok", False) and (previous_res.get("ok", True) if previous_res else True)
    return {
        "ok": all_ok,
        "current": current_res,
        "previous": previous_res,
    }


def open_portable_vault(*args, **kwargs):
    """Convenience helper to mount an arbitrary portable encrypted vault.

    Supported calling conventions:
      open_portable_vault(backend, vault_path, mount_path, password=..., ...)
      open_portable_vault(vault_path, mount_path, password=..., ...)
      open_portable_vault(vault_path=..., mount_path=..., ...)

    Unlock methods:
      - method='password' or password=...
      - method='recovery' or recovery_password=...
      - method='yubikey' (loads portable_enrollments from metadata without touching production store)
      - method='auto' (attempts password/recovery if given, falls back to YubiKey if enrollments present)
    """
    actual_backend = kwargs.get("backend")
    vpath = kwargs.get("svault_path") or kwargs.get("vault_path")
    mount_path = kwargs.get("mount_path")
    password = kwargs.get("password")
    recovery_password = kwargs.get("recovery_password")
    read_only = kwargs.get("read_only", True)
    method = kwargs.get("method", "auto")
    provider = kwargs.get("provider")
    pin_callback = kwargs.get("pin_callback")
    meta_path = kwargs.get("meta_path")

    if len(args) >= 3:
        actual_backend = args[0]
        vpath = args[1]
        mount_path = args[2]
    elif len(args) == 2:
        if hasattr(args[0], "unlock_and_mount"):
            actual_backend = args[0]
            vpath = args[1]
        else:
            vpath = args[0]
            mount_path = args[1]
    elif len(args) == 1:
        if hasattr(args[0], "unlock_and_mount"):
            actual_backend = args[0]
        else:
            vpath = args[0]

    if actual_backend is None:
        actual_backend = sa_storage.BitLockerVhdxBackend()

    if not vpath or not os.path.isfile(vpath):
        raise BackupError(f"Vault file not found: {vpath}", "file_not_found")

    # Locate sidecar metadata if needed
    if not meta_path:
        candidate_meta = vpath + ".meta"
        if os.path.isfile(candidate_meta):
            meta_path = candidate_meta
        else:
            dir_meta = os.path.join(os.path.dirname(vpath), DEFAULT_META_NAME)
            if os.path.isfile(dir_meta):
                meta_path = dir_meta

    manifest = {}
    if meta_path and os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            pass

    # Determine effective unlock method
    if method == "auto":
        if password is not None:
            method = "password"
        elif recovery_password is not None:
            method = "recovery"
        elif manifest.get("portable_enrollments"):
            method = "yubikey"
        else:
            raise BackupError("no unlock credentials or portable enrollments provided", "missing_credentials")

    if method == "yubikey":
        # Strict validation (R1)
        if not manifest:
            raise BackupError(f"Metadata file required for YubiKey unlock, missing or empty: {meta_path}", "metadata_missing")
        if not manifest.get("profile_id"):
            raise BackupError("Metadata missing profile_id required for cryptographic identity", "metadata_missing")
        if not manifest.get("container_id"):
            raise BackupError("Metadata missing container_id required for cryptographic identity", "metadata_missing")
        enrollment_dicts = manifest.get("portable_enrollments")
        if not enrollment_dicts:
            raise BackupError(
                f"No portable enrollments found in metadata ({meta_path}) for YubiKey unlock",
                "not_enrolled"
            )

        original_profile_id = manifest["profile_id"]
        original_container_id = manifest["container_id"]
        runtime_recovery_session_id = f"portable-recovery-{uuid.uuid4().hex[:8]}"

        class DisposableProfile:
            def __init__(self, container, m_path, c_id, prof_id, sess_id):
                self.id = prof_id
                self.session_id = sess_id
                self.container = container
                self.container_id = c_id
                self.mount_path = m_path
                self.backend = "bitlocker-vhdx"
                self.provider = "yubikey-fido2-hmac-secret"

        disp_prof = DisposableProfile(
            vpath, mount_path, original_container_id, original_profile_id, runtime_recovery_session_id
        )
        ephemeral_store = EphemeralCredentialStore(original_profile_id, original_container_id, enrollment_dicts)
        if provider is None:
            provider = sa_auth.YubiKeyFido2Provider(pin_callback=pin_callback)

        credential_id, volume_secret = sa_auth.unwrap_volume_secret(disp_prof, provider, ephemeral_store)
        try:
            # Sidecar Authenticity Verification (R10)
            manifest_hmac = manifest.get("manifest_hmac")
            sidecar_authenticated = "UNVERIFIED"
            if manifest_hmac:
                hmac_key_buf = sa_crypto.derive_manifest_hmac_key(volume_secret, original_container_id)
                try:
                    canonical_b = json.dumps({k: v for k, v in manifest.items() if k != "manifest_hmac"},
                                             sort_keys=True, separators=(',', ':')).encode("utf-8")
                    if not sa_crypto.verify_manifest_hmac(hmac_key_buf, canonical_b, manifest_hmac):
                        raise BackupError("Metadata HMAC validation failed after unwrap", "HMAC_MISMATCH")
                    sidecar_authenticated = "VERIFIED"
                finally:
                    hmac_key_buf.zeroize()
            else:
                if not kwargs.get("acknowledge_unverified", False) and not kwargs.get("allow_unverified", False):
                    raise BackupError(
                        "Sidecar metadata lacks authenticity HMAC. Explicit acknowledgement required.",
                        "SIDECAR_AUTHENTICITY_UNVERIFIED"
                    )
                sidecar_authenticated = "SIDECAR_AUTHENTICITY_UNVERIFIED"

            # H8: Verify actual payload SHA-256 against authenticated manifest before mounting
            expected_vhdx_sha = manifest.get("vhdx_sha256")
            if expected_vhdx_sha:
                actual_vhdx_sha = sha256_file(vpath)
                if actual_vhdx_sha.lower() != expected_vhdx_sha.lower():
                    raise BackupError(
                        f"Payload hash verification failed: manifest {expected_vhdx_sha} != actual {actual_vhdx_sha}",
                        "PAYLOAD_INTEGRITY_MISMATCH"
                    )
                payload_integrity = "VERIFIED"
            else:
                payload_integrity = "UNVERIFIED"

            mount_res = actual_backend.unlock_and_mount(
                disp_prof,
                volume_secret=volume_secret,
                read_only=read_only
            )
            if isinstance(mount_res, dict):
                mount_res["sidecar_authenticated"] = sidecar_authenticated
                mount_res["payload_integrity"] = payload_integrity
                mount_res["session_id"] = runtime_recovery_session_id
            return mount_res
        finally:
            if volume_secret is not None:
                sa_crypto.wipe(volume_secret)

    container_id = manifest.get("container_id", "portable_vault")
    profile_id = manifest.get("profile_id", "portable_vault")

    class DisposableProfile:
        def __init__(self, container, m_path, c_id, prof_id):
            self.id = prof_id
            self.container = container
            self.container_id = c_id
            self.mount_path = m_path
            self.backend = "bitlocker-vhdx"
            self.provider = "yubikey-fido2-hmac-secret"

    disp_prof = DisposableProfile(vpath, mount_path, container_id, profile_id)

    # H8: For password and recovery paths, verify actual VHDX SHA against manifest if metadata present
    expected_vhdx_sha = manifest.get("vhdx_sha256") if manifest else None
    if expected_vhdx_sha:
        actual_vhdx_sha = sha256_file(vpath)
        if actual_vhdx_sha.lower() != expected_vhdx_sha.lower():
            raise BackupError(
                f"Payload hash verification failed: manifest {expected_vhdx_sha} != actual {actual_vhdx_sha}",
                "PAYLOAD_INTEGRITY_MISMATCH"
            )
        payload_integrity = "PAYLOAD_INTEGRITY_VERIFIED"
        sidecar_authenticated = "SIDECAR_AUTHENTICITY_UNVERIFIED"
    else:
        if not kwargs.get("acknowledge_unverified", False) and not kwargs.get("allow_unverified", False):
            raise BackupError(
                "Sidecar metadata missing. Unverified recovery mode requires explicit acknowledgement.",
                "UNVERIFIED_RECOVERY_MODE"
            )
        payload_integrity = "UNVERIFIED"
        sidecar_authenticated = "NO_METADATA"

    if method == "password":
        if not password:
            raise BackupError("password required for portable password unlock", "missing_credentials")
        res = actual_backend.unlock_and_mount(
            disp_prof,
            password=password,
            read_only=read_only
        )
        if isinstance(res, dict):
            res["payload_integrity"] = payload_integrity
            res["sidecar_authenticated"] = sidecar_authenticated
        return res
    elif method == "recovery":
        if not recovery_password:
            raise BackupError("recovery password required for break-glass unlock", "missing_credentials")
        res = actual_backend.unlock_and_mount(
            disp_prof,
            recovery_password=recovery_password,
            read_only=read_only
        )
        if isinstance(res, dict):
            res["payload_integrity"] = payload_integrity
            res["sidecar_authenticated"] = sidecar_authenticated
        return res
    else:
        raise BackupError(f"Unsupported unlock method: {method}", "invalid_method")


def close_portable_vault(*args, **kwargs):
    """Detach a mounted portable vault.

    Supported calling conventions:
      close_portable_vault(backend, vault_path, mount_path)
      close_portable_vault(vault_path, mount_path)
      close_portable_vault(vault_path=..., mount_path=..., backend=...)
    """
    actual_backend = kwargs.get("backend")
    vpath = kwargs.get("svault_path") or kwargs.get("vault_path")
    mount_path = kwargs.get("mount_path")

    if len(args) >= 3:
        actual_backend = args[0]
        vpath = args[1]
        mount_path = args[2]
    elif len(args) == 2:
        if hasattr(args[0], "unmount"):
            actual_backend = args[0]
            vpath = args[1]
        else:
            vpath = args[0]
            mount_path = args[1]
    elif len(args) == 1:
        if hasattr(args[0], "unmount"):
            actual_backend = args[0]
        else:
            vpath = args[0]

    if actual_backend is None:
        actual_backend = sa_storage.BitLockerVhdxBackend()

    prof_id = kwargs.get("profile_id")
    if not prof_id and vpath:
        meta_cand = vpath + ".meta"
        if os.path.isfile(meta_cand):
            try:
                with open(meta_cand, "r", encoding="utf-8") as f:
                    prof_id = json.load(f).get("profile_id")
            except Exception:
                pass
    if not prof_id:
        prof_id = "portable_vault"

    class DisposableProfile:
        def __init__(self, container, m_path, p_id):
            self.id = p_id
            self.container = container
            self.mount_path = m_path

    disp_prof = DisposableProfile(vpath, mount_path, prof_id)
    return actual_backend.unmount(disp_prof)
