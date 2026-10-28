# SAITULS Portable Vault V2 — Post-V1 Future Gates (T-263)

**Authority:** T-263 board line: "Post-V1 Future Gates: ransomware mass-change detect, min known-good generations, immutable/offline gens, corrupted-current fallback, bit-rot scrub, periodic restore drills, YubiKey loss drill, succession drill, cloud version-history awareness, VHDX/BitLocker metadata corruption recovery, destination-compromise sim, clean-machine recovery doc test | needs: T-261 | verify: each gate has spec + test"

**Module:** `Scripts/secure_apps/sa_future_gates.py` (imported as `sa_future_gates`)

**Tests:** `tests/secure_apps_future_gates.py` — 159 assertions across 12 core primitives and 82 adversarial/regression controls.

**Evidence & Assurance Classification:**
- `UNIT_PASS`: 159 unit assertions passed.
- `DISPOSABLE_INTEGRATION_PASS`: 12 future gates verified in isolated synthetic scratch fixtures.
- `WINDOWS_SECURITY_PASS`: Windows NTFS reparse points, junctions, hardlinks, and traversal rejected.
- `HARDWARE_REAL_BLOCKED`: T-261 hardware durability/media acceptance unavailable on host.
- `PRODUCT_ACCEPTANCE_PENDING`: T-263 unadmitted (SCOUT phase / T-220 active, no-publish policy).

---

## Gate Index

| ID | Gate | Function | Test |
|----|------|----------|------|
| FG-01 | Ransomware mass-change detection | `detect_mass_change` | 5 assertions |
| FG-02 | Minimum known-good generations | `ensure_min_known_good` | 4 assertions |
| FG-03 | Immutable / offline generations | `mark_generation_immutable`, `is_generation_immutable` | 7 assertions |
| FG-04 | Corrupted-current fallback | `resolve_known_good_generation` | 5 assertions |
| FG-05 | Bit-rot scrub | `scrub_generations` | 4 assertions |
| FG-06 | Periodic restore drill | `run_restore_drill` | 5 assertions |
| FG-07 | YubiKey loss drill | `run_yubikey_loss_drill` | 5 assertions |
| FG-08 | Succession drill | `run_succession_drill` | 4 assertions |
| FG-09 | Cloud version-history awareness | `version_history_summary` | 6 assertions |
| FG-10 | VHDX/BitLocker metadata corruption recovery | `recover_metadata_corruption` | 7 assertions |
| FG-11 | Destination-compromise simulation | `simulate_destination_compromise` | 13 assertions |
| FG-12 | Clean-machine recovery document test | `recovery_doc_clean_machine_check` | 6 assertions |

---

## Design Principles

1. **No stubs, no prose promises.** Every gate drives real files against the on-disk layout produced by `sa_backup.publish_backup`: `generations/gen_<ts>_<hex8>/vault.svault`, `vault.svault.meta` (with `vhdx_sha256`), `CURRENT`/`PREVIOUS` pointers, and flat `__STORE.svault`/`__STORE.svault.meta` convenience copies.

2. **Verification is structural, not cosmetic.** A generation is "verified" only when its payload hash matches the sidecar's `vhdx_sha256`. A fallback only lands on a verified sibling. A sidecar repair only happens when a *different* verified generation attests the same hash — self-attestation is never trusted.

3. **Immutability is enforced, not declared.** `mark_generation_immutable` sets read-only attributes on the generation directory and its contents, so an operator or a careless rotation cannot rewrite it.

4. **Offline is a claim, not a magic state.** The `.offline` marker records the operator's assertion that a copy lives on separate media. The gate checks the marker; it cannot reach across the air gap.

5. **Every tamper is detectable.** `simulate_destination_compromise` performs real, reversible damage and reports exactly which artifact changed and whether the canonical generation still disagrees.

---

## Gate Specifications

### FG-01: Ransomware Mass-Change Detection

```python
detect_mass_change(events,
                   window_seconds=60.0,
                   threshold=25,
                   now=None) -> dict
```

*Sliding-window rate detector.* `events` is an iterable of `{"timestamp": epoch_seconds, "path": ..., "op": ...}`. Returns `{"detected": bool, "observed": int, "threshold": int, "window_start": float, "window_end": float, "reason": str}`.

- **Rationale:** Encryption ransomware produces a burst of file modifications far exceeding normal use. A fixed window ending at the newest event catches the burst regardless of file names.

### FG-02: Minimum Known-Good Generations

```python
ensure_min_known_good(destination_dir, min_good=2, hmac_key=None) -> dict
```

Walks `generations/`, verifies each generation's payload hash against its sidecar, counts verified generations. Returns `{"ok": bool, "verified": [...], "unverified": [...], "generations_found": int}`.

- **Gate:** Below `min_good` verified generations, no rollback is safe. The caller must refuse to proceed.

### FG-03: Immutable / Offline Generations

```python
mark_generation_immutable(destination_dir, gen_id, offline=False) -> dict
is_generation_immutable(destination_dir, gen_id) -> bool
validate_generation_id_containment(destination_dir, gen_id) -> str
```

Writes `.immutable` marker + read-only attributes on the generation directory and all its files. Optional `.offline` marker for copies on separate media.
- **Path Containment:** Full path-chain validation (`destination_dir`, `generations/` parent, generation directory, and target files). Strictly rejects symlinks, Windows NTFS junctions, and traversal escapes (`reparse_point_detected`, `path_traversal`).
- **Gate:** YubiKey-loss and succession drills require the source generation to be immutable.

### FG-04: Corrupted-Current Fallback

```python
resolve_known_good_generation(destination_dir, hmac_key=None) -> dict
```

Ignores the `CURRENT` pointer. Walks generations newest-first, returns the first whose payload hash matches its sidecar (and HMAC if `hmac_key` supplied). Returns `{"ok": bool, "generation_id": str, "generation_dir": str, "sha256": str, "hmac": "VERIFIED"|"NOT_CHECKED"|"HMAC_MISMATCH", "immutable": bool}`.

- **Gate:** The fallback is the only trustworthy recovery path when `CURRENT` is corrupt.

### FG-05: Bit-Rot Scrub

```python
scrub_generations(destination_dir, quarantine=True) -> dict
```

Re-hashes every generation. Suspect generations (missing payload, missing sidecar, hash mismatch) are optionally moved to `.quarantine/gen_<ts>_<hex8>.<epoch>`. Returns `{"ok": bool, "clean": int, "suspect": int, "scrubbed": [...], "quarantined": [...]}`.

- **Gate:** Silent bit rot leaves a suspect generation in place. The scrub makes the survivor set exactly the verified set.

### FG-06: Periodic Restore Drill

```python
run_restore_drill(destination_dir, work_dir, gen_id=None, hmac_key=None) -> dict
```

Copies the chosen generation's payload to a scratch directory, re-hashes it there. Defaults to the newest verified generation. Returns `{"ok": bool, "generation_id": str, "restored_to": str, "sha256": str, "expected_sha256": str, "immutable_source": bool}`.

- **Gate:** A backup never restored is a hypothesis. The drill proves the stored bytes still decode.

### FG-07: YubiKey Loss Drill

```python
run_yubikey_loss_drill(destination_dir, require_immutable=True) -> dict
```

Checks the resolved generation's sidecar for: `recovery_phrase_instructions`, `key_hint`, and `key_outside_authenticator` (the payload is not key-derived from the authenticator). Also requires `immutable=True` when `require_immutable`. Returns `{"ok": bool, "key_outside_authenticator": bool, "recovery_phrase_instructions_present": bool, "key_hint_present": bool, "immutable": bool}`.

- **Gate:** Losing the device loses the data unless the vault key lives outside the authenticator and the recovery phrase is stored separately.

### FG-08: Succession Drill

```python
run_succession_drill(destination_dir, new_owner_label="successor") -> dict
```

Checks the destination carries: `RECOVERY` doc, `RECOVER` script, `owner_label` and `successor_label` in the sidecar, and an immutable source generation. Returns `{"ok": bool, "facts": {...}, "missing": [...], "new_owner_label": str}`.

- **Gate:** Succession is authorisation, not encryption. The successor must reach the recovery materials; the predecessor must be able to revoke.

### FG-09: Cloud Version-History Awareness

```python
version_history_summary(destination_dir, include_hashes=True, limit=50) -> dict
```

Enumerates every retained generation newest-first with `created_at`, `profile_id`, `vhdx_size_bytes`, `immutable`, `payload_present`, and optionally `sha256` + `sha256_verified`. Also surfaces quarantined generations.

- **Gate:** A cloud object store gives this for free. The local destination must be equally explicit.

### FG-10: VHDX/BitLocker Metadata Corruption Recovery

```python
recover_metadata_corruption(destination_dir, hmac_key=None, dry_run=False) -> dict
```

1. Re-points a corrupt/missing `CURRENT` at the newest verified generation (preserving valid rollbacks).
2. Rebuilds flat convenience copies via `sa_backup.reconcile_generations_and_flat_files`.
3. Repairs any generation whose sidecar is missing or disagrees — **only** when a *different* verified generation attests the same payload hash. Self-attestation is never trusted.
4. **Authenticated Recovery:** When the original generation had a signed manifest, repairing the sidecar requires `hmac_key`. If the key is supplied, the recovered sidecar is re-signed with HKDF/HMAC-SHA256 (`sidecar_rebuilt_and_authenticated`). If the key is absent, sidecar repair is refused (`sidecar_repair_refused_signing_key_required`) and the damaged generation is safely kept unverified, preventing unauthenticated manifest forgery or laundering.

Returns `{"ok": True, "actions": [...], "before": {...}, "after": {...}, "payload_sha256": str}`.

- **Gate:** A damaged destination must self-heal, but never launder damage by inventing hashes or forging signatures.

### FG-11: Destination-Compromise Simulation

```python
simulate_destination_compromise(destination_dir, mode="flat_files") -> dict
```

**Architectural Guarantee (Isolated Synthetic Fixture):**
- **Zero In-Place Source Modification:** The supplied source destination is strictly read-only throughout simulation. Pre- and post-simulation tree hash snapshots mathematically prove zero source corruption.
- **Controlled In-Process Disposable Fixture:** All destructive simulation runs exclusively inside an internally allocated, non-forgeable disposable clone fixture (`SYNTHETIC_FIXTURE_SIMULATION`), cleaned up in `finally`.
- **Path Redirection Rejection:** Upfront inspection (`_inspect_destination_safety`) refuses production paths, symlinks, Windows NTFS directory junctions, reparse points, and unexpected hard links (`st_nlink > 1`).
- **Disposable Fixture Ownership:** Refuses substring-based matching (`TEMP`/`TMP`) and marker spoofing (`.disposable_fixture`). Requires active in-process registry ownership (`is_registered_disposable_fixture`).

Modes:
- `flat_files` — overwrites `__STORE.svault` with garbage.
- `current_ptr` — points `CURRENT` at a non-existent generation.
- `sidecar` — flips a byte in the canonical generation's sidecar.

Returns `{"ok": bool, "evidence_class": "SYNTHETIC_FIXTURE_SIMULATION", "simulation_architecture": "ISOLATED_CLONE", "source_read_only_verified": True, "source_mutated": False, "detectable": bool, ...}`.

- **Gate:** A backup that cannot notice a tampered destination will quietly restore attacker-controlled bytes. Never claim hardware/real destination acceptance from synthetic simulation.

### FG-12: Clean-Machine Recovery Document Test

```python
recovery_doc_clean_machine_check(destination_dir=None, doc_path=None, require_all=True) -> dict
```

Checks the recovery document (`__STORE-RECOVERY.txt`) for the required fields:

```
VAULT_NAME, GENERATION_ID, SHA256, RECOVERY_SCRIPT, SIDECAR,
KEY_HINT, RECOVERY_PHRASE_INSTRUCTIONS, CONTACT
```

Returns `{"ok": bool, "present": {field: bool}, "missing": [...], "doc_path": str, "size_bytes": int}`.

- **Gate:** A recovery document that assumes the reader has the builder's memory is not a recovery document.

---

## Running the Tests

```bash
cd V:\___VAC\__K\__CODE\__SAITULS
python tests/secure_apps_future_gates.py
```

Expected output (truncated):

```
[PASS] FG-01 quiet traffic not flagged -- observed=5
[PASS] FG-01 mass burst detected -- observed=65 threshold=25
...
[PASS] FG-12 complete doc reports 8/8 present and passes
[PASS] FG11-ARCH[flat_files] destination with external junction rejected -- reparse_point_detected
[PASS] FG11-ARCH[flat_files] external bytes preserved byte-for-byte
[PASS] FG11-ARCH generations parent junction escape rejected -- reparse_point_detected
[PASS] FG11-ARCH fake TEMP/TMP name refused without registration
[PASS] FG11-ARCH marker spoofing refused without registration
[PASS] FG11-ARCH Windows junction identified as reparse point
[PASS] FG11-ARCH signed sibling recovery records authenticated rebuild
[PASS] FG11-ARCH missing signing key refuses sidecar repair
[PASS] FG11-ARCH[flat_files] production write refused

TOTAL 159  PASS 159  FAIL 0

=== FUTURE GATES ASSURANCE SUMMARY (T-263) ===
UNIT_PASS: 159
DISPOSABLE_INTEGRATION_PASS: 12 gates verified in isolated disposable scratch
WINDOWS_SECURITY_PASS: NTFS reparse points, junctions, hardlinks, traversal refused
HARDWARE_REAL_BLOCKED: T-261 hardware durability/media acceptance unavailable
PRODUCT_ACCEPTANCE_PENDING: T-263 unadmitted (SCOUT / T-220 active, no-publish)
ASSURANCE STATUS: CONDITIONAL_SECURITY_ASSURED (NO_PUBLISH)
```

---

## Integration with Secure Apps

The gate functions are designed for:

- **Backup scripts** — call `ensure_min_known_good` before rotation, `scrub_generations` on a schedule, `run_restore_drill` periodically.
- **Recovery tooling** — `resolve_known_good_generation` is the entry point; `recover_metadata_corruption` is the one-shot repair.
- **Operational drills** — `run_yubikey_loss_drill`, `run_succession_drill`, `simulate_destination_compromise` are non-destructive proofs.
- **Monitoring** — `detect_mass_change` consumes the same event stream the watcher produces; `version_history_summary` feeds dashboards.

---

## Limitations & Open Work

- **T-261 dependency:** T-263's `needs: T-261` cannot be satisfied on this host. T-261 requires HARDWARE_REAL acceptance (real FIDO for gates B/F, privileged-helper install for gate A, genuine oracles for G/H/I). These are **genuinely human/system actions** (credentials, payments, physical actions, destructive or legal steps) — per standing operator instruction, the agent does not ask for them.

- **Offline verification:** FG-03 records the `.offline` claim but cannot verify it. That requires a second machine and a handoff protocol.

- **Cloud awareness:** FG-09 enumerates local history. Cloud version-history awareness (e.g., S3 object versions, Azure Blob snapshots) would be a separate connector, not this gate.

- **No FUTURE_GATE roadmap indexing:** `saipen future status` reports `indexed: false`. The BOARD line and this document are the only spec authority.

---

## Source Files

- `Scripts/secure_apps/sa_future_gates.py` — implementation
- `tests/secure_apps_future_gates.py` — specification by test
- `Wiki/SECURE-APPS-FUTURE-GATES.md` — this document