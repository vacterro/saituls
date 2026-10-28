# SAITULS Secure Apps — Production Cutover & Hardening Plan

**Status**: P0 STAGED — Code & Hermetic Gates 100% GREEN  
**Linage**: T-257 (Portable Vault Implementation) -> T-258 (Post-V1 Hardening & Live Acceptance)  
**Safety Invariant**: Real production Obsidian vault and `V:\___VAC\__K\__CONFIG\_FFS\main_sync.*` remain completely untouched until this protocol is executed interactively by the operator.

---

## 1. Verified Architecture & Invariants

1. **Payload**: Single canonical BitLocker XTS-AES-256 encrypted VHDX payload.
2. **Access Doors**:
   - **Door 1 (Daily)**: YubiKey / FIDO2 HMAC-Secret unwrap.
   - **Door 2 (Maintenance/Portable)**: Native BitLocker `Password` protector with transactional escrow rollback.
   - **Door 3 (Break-Glass)**: Native BitLocker 48-digit `RecoveryPassword` / reversible BIP-39 mnemonic.
3. **Transport**: FreeFileSync transfers ONLY ciphertext (`.svault` and `.meta`). All plaintext mounts (`\__STORE\`, `*\__STORE\`, `*.partial`) are strictly excluded in `MainConfig/Filter/Exclude` and per-pair custom filters.
4. **Backup Generations**: Immutable dual-generation rotation with atomic `CURRENT` pointer and HMAC-SHA256 authenticated sidecar metadata.

---

## 2. Completed Post-V1 Hardening & Defect Closures (T-258)

- **H1 (Disposable Live Acceptance Runner)**:
  Created `tests/secure_apps_portable_live.py` covering Doors LIVE A through LIVE I. Integrated into `tests/secure_apps_interactive.py --portable-vault-acceptance`.
- **H2 & H15 (BitLocker Password Protector Limits & Legacy Layout)**:
  BitLocker enforces at most one `Password` protector per volume. In `sa_broker.upgrade_legacy_vault`, addition of the new portable password is now safely deferred until the legacy daily password protector is retired, avoiding native failure. Ambiguous legacy layouts with more than 1 recovery protector fail closed.
- **H3 & H4 (GUI & CLI Password Rotation Hardening)**:
  - GUI blocks password change/removal while the protected application is running (`RUNNING`).
  - Added current password entry field in `ChangePortablePasswordDialog` and `--old-password` in CLI for atomic rollback and escrow verification.
- **H6 & H7 (Explicit Authenticated vs Integrity-Only Backup State)**:
  - `publish_backup(..., allow_integrity_only=False)` fails closed if session or HMAC key is absent.
  - Distinguishes runtime state between `BACKUP_CURRENT_AUTHENTICATED` (required for cutover) and `BACKUP_CURRENT_INTEGRITY_ONLY`.
- **H11 & H14 (Staging & Standalone Recovery Script Hygiene)**:
  - Junction and reparse point defense in `.staging` cleanup.
  - Hardened standalone PowerShell recovery script `__STORE-RECOVER.ps1` verifies payload SHA-256 against sidecar metadata before attaching.
- **FFS Authoritative Filter Fix**:
  Hardening modifies authoritative `MainConfig/Filter/Exclude` XML nodes and validates pair-level filters, preventing false-green security bypass.

---

## 3. Operator Execution Protocol (Step-by-Step)

### Phase 1: Real Disposable Live Acceptance (Hardware Gate)
*Requirements: Windows 10/11 Pro/Enterprise, Administrator terminal, physical YubiKey inserted.*

1. Launch elevated PowerShell in repository root:
   ```powershell
   cd V:\___VAC\__K\__CODE\__SAITULS
   python tests/secure_apps_portable_live.py
   ```
2. Verify all 9 gates pass with physical BitLocker:
   - `LIVE A (Disposable V2 Creation)`: PASS
   - `LIVE B (Daily Door 2x Cycle)`: PASS
   - `LIVE C (Portable Password Maintenance Rotation)`: PASS
   - `LIVE D (Native Door 2 Without SAITULS)`: PASS
   - `LIVE E (Native Door 3 Without SAITULS)`: PASS
   - `LIVE F (Sidecar-Only Recovery)`: PASS
   - `LIVE G (Generation Semantics & Crash Recovery)`: PASS
   - `LIVE H (Backup / Open Race)`: PASS
   - `LIVE I (Real Disposable FFS Preview & Cutover Gate)`: PASS

### Phase 2: FreeFileSync Configuration Hardening
*Applies security rules to operator's live sync files.*

1. Run FFS hardening CLI:
   ```powershell
   python Scripts/secure_apps/sa_cli.py ffs harden --batch "V:\___VAC\__K\__CONFIG\_FFS\main_sync.ffs_batch" --gui "V:\___VAC\__K\__CONFIG\_FFS\main_sync.ffs_gui"
   ```
2. Run audit check:
   ```powershell
   python Scripts/secure_apps/sa_cli.py ffs audit --batch "V:\___VAC\__K\__CONFIG\_FFS\main_sync.ffs_batch" --gui "V:\___VAC\__K\__CONFIG\_FFS\main_sync.ffs_gui"
   ```
3. Open FreeFileSync GUI, inspect Filter settings:
   - Verify `\__STORE\` and `*.partial` appear in Exclude list.
   - Run Compare on `main_sync.ffs_gui`: verify no plaintext files under `__STORE\` are queued for transport.

### Phase 3: Initial Canonical Encrypted Publication
1. Ensure Obsidian is closed.
2. Publish initial authenticated backup generation:
   ```powershell
   python Scripts/secure_apps/sa_cli.py backup publish --profile obsidian
   ```
3. Verify published state:
   ```powershell
   python Scripts/secure_apps/sa_cli.py backup verify --profile obsidian
   ```

### Phase 4: Production Vault V2 Migration
*Transition legacy Obsidian vault to V2 layout with Door 1, 2, and 3.*
1. Upgrade legacy vault protectors:
   ```powershell
   python Scripts/secure_apps/sa_cli.py upgrade-legacy --profile obsidian
   ```
2. Set Portable Master Password:
   ```powershell
   python Scripts/secure_apps/sa_cli.py portable-password set --profile obsidian
   ```
3. Print recovery guide and 48-digit key for offline physical storage.

### Phase 5: Plaintext Backup Retirement Gate
- Plaintext copies on external disks (`R:`, `Z:`, etc.) must **NOT** be deleted until at least 3 consecutive authenticated encrypted generations have been verified and restored successfully.
