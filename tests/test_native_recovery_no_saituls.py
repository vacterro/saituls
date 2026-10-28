# encoding: utf-8
"""Acceptance Test: Native Windows Recovery Without SAITULS (P1-5).

Proves that a copied disposable .svault can be recovered using ONLY built-in
Windows cmdlets:
- Mount-DiskImage
- Get-Disk, Get-Partition, Get-Volume, Set-Partition
- Unlock-BitLocker (Door 2: Portable Master Password)
- Unlock-BitLocker (Door 3: 48-digit Recovery Key)
- Dismount-DiskImage

Zero imports from Scripts/secure_apps in the recovery phase.
Zero broker, zero helper, zero credential store.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))

import sa_crypto
import sa_storage
import sa_backup


POWERSHELL_RECOVERY_SCRIPT = r"""
param(
    [string]$VaultPath,
    [string]$Password,
    [string]$RecoveryKey,
    [string]$ExpectedCanarySha256
)

$ErrorActionPreference = "Stop"

function Test-RecoverWithMethod([string]$Method, [string]$Secret) {
    Write-Host "[*] Testing native recovery with method: $Method"
    $fullPath = [System.IO.Path]::GetFullPath($VaultPath)
    if (-not (Test-Path -LiteralPath $fullPath)) {
        throw "Vault file not found: $fullPath"
    }

    $attachPath = $fullPath
    $tempCopy = $null
    if ($fullPath -notmatch '\.(vhdx|vhd)$') {
        $tempDir = Join-Path $env:TEMP ("svault_native_test_" + [System.Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
        $tempCopy = Join-Path $tempDir "vault.vhdx"
        Copy-Item -LiteralPath $fullPath -Destination $tempCopy -Force
        $attachPath = $tempCopy
    }

    $attached = $false
    try {
        $img = Mount-DiskImage -ImagePath $attachPath -NoDriveLetter -PassThru -ErrorAction Stop
        $attached = $true
        $disk = Get-Disk -Number $img.Number
        $part = $disk | Get-Partition | Where-Object { $_.Type -ne 'Reserved' } | Select-Object -First 1
        if (-not $part) { throw "No data partition found" }

        $vol = $part | Get-Volume
        $driveLetter = $null
        if ($vol -and $vol.DriveLetter) {
            $driveLetter = "$($vol.DriveLetter):"
        } else {
            $used = Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Name
            $available = 68..90 | ForEach-Object { [char]$_ } | Where-Object { $used -notcontains $_ } | Select-Object -Last 1
            if (-not $available) { throw "No drive letter available" }
            Set-Partition -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber -NewDriveLetter $available | Out-Null
            $driveLetter = "$($available):"
        }

        Write-Host "    Volume mapped at $driveLetter. Unlocking with $Method..."
        if ($Method -eq "Password") {
            $sec = ConvertTo-SecureString $Secret -AsPlainText -Force
            Unlock-BitLocker -MountPoint $driveLetter -Password $sec -ErrorAction Stop | Out-Null
        } elseif ($Method -eq "RecoveryPassword") {
            Unlock-BitLocker -MountPoint $driveLetter -RecoveryPassword $Secret.Trim() -ErrorAction Stop | Out-Null
        } else {
            throw "Unknown method: $Method"
        }

        $bl = Get-BitLockerVolume -MountPoint $driveLetter
        if ("$($bl.LockStatus)" -ne "Unlocked") {
            throw "BitLocker LockStatus is not Unlocked: $($bl.LockStatus)"
        }

        # Check canary file
        $canaryFile = Join-Path $driveLetter "canary.txt"
        if (-not (Test-Path -LiteralPath $canaryFile)) {
            throw "Canary file missing in unlocked volume: $canaryFile"
        }
        $canaryBytes = [System.IO.File]::ReadAllBytes($canaryFile)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $hashBytes = $sha.ComputeHash($canaryBytes)
        $computedSha = -join ($hashBytes | ForEach-Object { $_.ToString("x2") })

        if ($computedSha -ne $ExpectedCanarySha256.ToLower()) {
            throw "Canary SHA mismatch! Expected $ExpectedCanarySha256, got $computedSha"
        }
        Write-Host "    [+] Canary verified: $computedSha"
        return $true
    } finally {
        if ($attached) {
            Dismount-DiskImage -ImagePath $attachPath -ErrorAction SilentlyContinue | Out-Null
        }
        if ($tempCopy -and (Test-Path -LiteralPath (Split-Path $tempCopy))) {
            Remove-Item -LiteralPath (Split-Path $tempCopy) -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# Run Door 2: Portable Password
Test-RecoverWithMethod -Method "Password" -Secret $Password

# Run Door 3: Recovery Key
Test-RecoverWithMethod -Method "RecoveryPassword" -Secret $RecoveryKey

Write-Host "[+] ALL NATIVE NO-SAITULS TESTS PASSED!"
"""


class TestNativeRecoveryNoSaituls(unittest.TestCase):
    """Authoritative Door 2 and Door 3 native Windows recovery proof."""

    @classmethod
    def setUpClass(cls):
        import ctypes
        if not bool(ctypes.windll.shell32.IsUserAnAdmin()):
            raise unittest.SkipTest(
                "Native Windows recovery requires an elevated administrator session (BitLocker cmdlets require admin rights)"
            )
        cls.test_dir = r"V:\___VAC\__K\_TEMP_NATIVE_RECOVERY_TEST"
        cls.container_path = os.path.join(cls.test_dir, "native_probe.vhdx")
        cls.mount_path = os.path.join(cls.test_dir, "mount")
        cls.backup_path = os.path.join(cls.test_dir, "native_probe.svault")

        # Cleanup any previous artifacts
        backend = sa_storage.BitLockerVhdxBackend()
        class DummyProfile:
            def __init__(self, c_path, m_path):
                self.id = "native_probe"
                self.container_id = "native-container-123"
                self.container = c_path
                self.mount_path = m_path
                self.size_gb = 1
                self.filesystem_label = "NATIVE_STORE"
        mock_p = DummyProfile(cls.container_path, cls.mount_path)
        try:
            backend.unmount(mock_p)
        except Exception:
            pass
        shutil.rmtree(cls.test_dir, ignore_errors=True)
        os.makedirs(cls.test_dir, exist_ok=True)
        os.makedirs(cls.mount_path, exist_ok=True)

        cls.portable_password = "Native-Windows-Portable-Passphrase-99"
        cls.volume_secret = sa_crypto.new_volume_secret()

        # Step 1: Create disposable real BitLocker VHDX fixture
        create_res = backend.create(
            profile=mock_p,
            volume_secret=cls.volume_secret,
            portable_password=cls.portable_password
        )
        cls.recovery_key = create_res.get("recovery_password")
        assert cls.recovery_key, "Failed to create BitLocker recovery key"

        # Step 2: Write canary into volume
        cls.canary_content = b"NATIVE_RECOVERY_VERIFICATION_PAYLOAD_CANARY_2026"
        cls.canary_sha256 = hashlib.sha256(cls.canary_content).hexdigest().lower()
        canary_path = os.path.join(cls.mount_path, "canary.txt")
        with open(canary_path, "wb") as f:
            f.write(cls.canary_content)

        # Step 3: Unmount container completely
        backend.unmount(mock_p)

        # Step 4: Copy to .svault extension to test arbitrary extension recovery
        shutil.copyfile(cls.container_path, cls.backup_path)

        # Step 5: Save native PowerShell recovery test script
        cls.script_path = os.path.join(cls.test_dir, "test_native_recovery.ps1")
        with open(cls.script_path, "w", encoding="utf-8") as f:
            f.write(POWERSHELL_RECOVERY_SCRIPT)

    @classmethod
    def tearDownClass(cls):
        # Cleanup
        try:
            subprocess.run(["pwsh", "-NoProfile", "-Command",
                            f"Dismount-DiskImage -ImagePath '{cls.container_path}' -ErrorAction SilentlyContinue"],
                           capture_output=True)
        except Exception:
            pass
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def test_native_windows_recovery_powershell(self):
        """Execute native recovery script in pure PowerShell without SAITULS imports."""
        cmd = [
            "pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", self.script_path,
            "-VaultPath", self.backup_path,
            "-Password", self.portable_password,
            "-RecoveryKey", self.recovery_key,
            "-ExpectedCanarySha256", self.canary_sha256,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        print("STDOUT:", proc.stdout)
        print("STDERR:", proc.stderr)
        self.assertEqual(proc.returncode, 0, f"Native recovery script failed with code {proc.returncode}: {proc.stderr}")
        self.assertIn("ALL NATIVE NO-SAITULS TESTS PASSED!", proc.stdout)


class TestPortableYubiKeyRecoverySidecarOnly(unittest.TestCase):
    """Oracle B: Portable YubiKey Recovery using ONLY sidecar metadata, zero local store."""

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.mkdtemp(prefix="sa_test_sidecar_only_")
        self.dest_dir = os.path.join(self.temp_dir, "backups")
        os.makedirs(self.dest_dir, exist_ok=True)
        self.vault_file = os.path.join(self.temp_dir, "vault.vhdx")
        with open(self.vault_file, "wb") as f:
            f.write(b"MOCK_BITLOCKER_PAYLOAD_CANARY" * 512)

        self.profile = MagicMock()
        self.profile.id = "obsidian"
        self.profile.container_id = "target-container-id-999"
        self.profile.container = self.vault_file
        self.profile.backup_destination = self.dest_dir
        self.profile.credential_profile = "primary"

        self.backend = sa_storage.FakeStorageBackend()
        self.key = self.backend._key(self.profile)
        self.backend._exists[self.key] = True
        self.backend._state[self.key] = sa_storage.DETACHED

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_oracle_b_yubikey_sidecar_recovery_without_local_store(self):
        """Door 1: Sidecar envelope + YubiKey unlocks vault when local CredentialStore is destroyed."""
        import sa_auth
        provider = sa_auth.FakeAuthProvider()

        # Step 1: Create secret and enrollment in temporary store
        secret = sa_crypto.new_volume_secret()
        self.backend._secret[self.key] = secret.bytes()

        cred = provider.create_credential(self.profile.credential_profile)
        enrollment = sa_auth.wrap_volume_secret_for(
            self.profile, provider, cred, secret, container_id=self.profile.container_id
        )

        orig_store_path = os.path.join(self.temp_dir, "original_store.json")
        store = sa_auth.CredentialStore(orig_store_path)
        store.add(self.profile.id, self.profile.container_id, enrollment)

        # Step 2: Publish backup with sidecar metadata and HMAC
        hmac_key = sa_crypto.derive_manifest_hmac_key(secret, self.profile.container_id)
        pub_res = sa_backup.publish_backup(
            self.profile,
            self.backend,
            destination_dir=self.dest_dir,
            hmac_key=hmac_key,
            cred_store=store
        )
        self.assertTrue(pub_res.get("ok"))
        backup_vault = pub_res["vault_path"]
        backup_meta = pub_res["meta_path"]

        # Step 3: Destroy original store completely
        os.remove(orig_store_path)
        self.assertFalse(os.path.exists(orig_store_path))

        # Step 4: Open portable vault using ONLY backup files and connected YubiKey
        mount_target = os.path.join(self.temp_dir, "mounted_vault")
        mount_res = sa_backup.open_portable_vault(
            self.backend,
            backup_vault,
            mount_target,
            method="yubikey",
            provider=provider,
            meta_path=backup_meta
        )
        self.assertTrue(mount_res.get("ok", True))
        self.assertEqual(mount_res.get("state"), sa_storage.MOUNTED)
        self.assertEqual(mount_res.get("sidecar_authenticated"), "VERIFIED")

        # Step 5: Cleanly close
        close_res = sa_backup.close_portable_vault(self.backend, backup_vault, mount_target)
        self.assertEqual(close_res.get("state"), sa_storage.DETACHED)


if __name__ == "__main__":
    unittest.main()
