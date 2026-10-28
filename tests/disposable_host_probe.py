# encoding: utf-8
"""Disposable Host BitLocker & Backup Verification Probe.

Proves real BitLocker data volume creation, Door 1 (daily recovery key),
Door 2 (portable master password), Door 3 (break-glass recovery key),
ciphertext backup publishing, and recovery on the real host.
All temporary assets are strictly placed in V:\___VAC\__K\_TEMP_DISPOSABLE_PROBE
and cleaned up upon completion.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_crypto
import sa_storage
import sa_backup


def main():
    probe_dir = r"V:\___VAC\__K\_TEMP_DISPOSABLE_PROBE"
    container_path = os.path.join(probe_dir, "probe_vault.vhdx")
    mount_path = os.path.join(probe_dir, "mount")
    backup_dir = os.path.join(probe_dir, "backup")
    recover_mount = os.path.join(probe_dir, "recover_mount")

    print("[*] Starting Disposable Host BitLocker & Backup Probe...")
    print(f"[*] Probe directory: {probe_dir}")

    backend = sa_storage.BitLockerVhdxBackend()

    mock_profile = MagicMock()
    mock_profile.id = "disposable_probe"
    mock_profile.container_id = "probe-container-123"
    mock_profile.container = container_path
    mock_profile.mount_path = mount_path
    mock_profile.size_gb = 1
    mock_profile.filesystem_label = "PROBE_STORE"
    mock_profile.backup_destination = backup_dir

    # Ensure clean starting state
    if os.path.exists(probe_dir):
        try:
            backend.unmount(mock_profile)
        except Exception:
            pass
        try:
            sa_backup.close_portable_vault(os.path.join(backup_dir, "__STORE.svault"), recover_mount)
        except Exception:
            pass
        shutil.rmtree(probe_dir, ignore_errors=True)

    os.makedirs(probe_dir, exist_ok=True)
    os.makedirs(mount_path, exist_ok=True)
    os.makedirs(backup_dir, exist_ok=True)
    os.makedirs(recover_mount, exist_ok=True)

    portable_password = "Disposable-Master-Password-Probe-9988"
    volume_secret = sa_crypto.new_volume_secret()

    print("[1/7] Creating disposable 1 GB BitLocker VHDX with 3 Doors on real host...")
    create_res = backend.create(
        profile=mock_profile,
        volume_secret=volume_secret,
        portable_password=portable_password
    )
    print("      Create Result:", json.dumps({k: ("[REDACTED]" if "password" in k and k != "has_portable_password" else v) for k, v in create_res.items()}))
    break_glass_key = create_res.get("recovery_password")
    daily_key = create_res.get("daily_recovery_password")
    assert break_glass_key, "Missing break-glass recovery password"
    assert daily_key, "Missing daily recovery password"
    assert create_res.get("has_portable_password"), "Portable password protector missing"

    # Write canary file into mounted vault
    canary_path = os.path.join(mount_path, "canary_test.txt")
    canary_data = "CANARY_PAYLOAD_BITLOCKER_AUTHENTICATED_VAULT_CONTENT"
    with open(canary_path, "w", encoding="utf-8") as f:
        f.write(canary_data)
    print("      Canary written successfully into mounted volume.")

    # List protectors live
    prots = backend.list_protectors(mock_profile)
    print(f"      Protectors on volume ({len(prots.get('protectors', []))}):")
    for p in prots.get("protectors", []):
        ptype = p.get('type') or p.get('ProtectorType')
        pid = p.get('id') or p.get('KeyProtectorId')
        print(f"        • Type: {ptype} | ID: {pid}")

    # Unmount container
    print("[2/7] Detaching volume to test Door 2 (Portable Password unlock)...")
    unm_res = backend.unmount(mock_profile)
    print(f"      Detached. State: {unm_res.get('state')}")

    # Test Door 2 unlock
    print("[3/7] Unlocking using Door 2 (Portable Master Password)...")
    unlock_res = backend.unlock_and_mount(
        profile=mock_profile,
        password=portable_password
    )
    print(f"      Door 2 Unlock State: {unlock_res.get('state')}")
    with open(canary_path, "r", encoding="utf-8") as f:
        read_data = f.read()
    assert read_data == canary_data, "Canary data mismatch after Door 2 unlock!"
    print("      [+] Door 2 (Portable Master Password) VERIFIED!")

    backend.unmount(mock_profile)

    # Test Door 3 unlock
    print("[4/7] Testing Door 3 (Break-Glass Recovery Password unlock)...")
    unlock_rec = backend.unlock_and_mount(
        profile=mock_profile,
        recovery_password=break_glass_key
    )
    print(f"      Door 3 Unlock State: {unlock_rec.get('state')}")
    with open(canary_path, "r", encoding="utf-8") as f:
        read_data = f.read()
    assert read_data == canary_data, "Canary data mismatch after Door 3 unlock!"
    print("      [+] Door 3 (Break-Glass Recovery Key) VERIFIED!")

    backend.unmount(mock_profile)

    # Test Door 1 unlock
    print("[5/7] Testing Door 1 (Daily FIDO-wrapped recovery secret unlock)...")
    unlock_daily = backend.unlock_and_mount(
        profile=mock_profile,
        recovery_password=daily_key
    )
    print(f"      Door 1 Unlock State: {unlock_daily.get('state')}")
    with open(canary_path, "r", encoding="utf-8") as f:
        read_data = f.read()
    assert read_data == canary_data, "Canary data mismatch after Door 1 unlock!"
    print("      [+] Door 1 (Daily Recovery Secret) VERIFIED!")

    backend.unmount(mock_profile)

    # Step 6: Publish Encrypted Backup
    print("[6/7] Publishing encrypted backup (__STORE.svault)...")
    pub_res = sa_backup.publish_backup(
        profile=mock_profile,
        backend=backend,
        destination_dir=backup_dir
    )
    print(f"      Backup published: {pub_res.get('vault_path')}")
    print(f"      SHA-256: {pub_res.get('sha256')}")
    print(f"      Size: {pub_res.get('size_bytes')} bytes in {pub_res.get('duration_seconds'):.2f}s")

    # Verify published backup
    verify_res = sa_backup.verify_backup(destination_dir=backup_dir)
    assert verify_res.get("ok"), f"Backup verification failed: {verify_res}"
    print("      [+] Backup SHA-256 and metadata VERIFIED!")

    # Step 7: Open Portable Vault from Backup using Door 2
    print("[7/7] Testing recovery from backup file using Portable Password (read-only)...")
    rec_res = sa_backup.open_portable_vault(
        vault_path=pub_res["vault_path"],
        mount_path=recover_mount,
        password=portable_password,
        read_only=True
    )
    print(f"      Recover Mount State: {rec_res.get('state')}")
    with open(os.path.join(recover_mount, "canary_test.txt"), "r", encoding="utf-8") as f:
        backup_canary = f.read()
    assert backup_canary == canary_data, "Backup canary data mismatch!"
    print("      [+] Recovered canary data from published backup matches perfectly!")

    # Close backup mount
    sa_backup.close_portable_vault(pub_res["vault_path"], recover_mount)
    print("      Recover mount closed cleanly.")

    # Cleanup disposable assets
    print("[*] Cleaning up disposable probe directory...")
    time.sleep(1.0)
    shutil.rmtree(probe_dir, ignore_errors=True)
    print("[+] Cleanup complete. Host BitLocker & Backup Probe PASSED with 100% SUCCESS!")


if __name__ == "__main__":
    main()
