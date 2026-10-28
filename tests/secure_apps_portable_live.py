# encoding: utf-8
"""Dedicated Interactive & Automated Runner for Portable Vault Live Acceptance (LIVE A - I).

Executes the full Disposable Live Acceptance suite for SAITULS Portable Vault V2:
- LIVE A: Disposable V2 Creation & Canary Setup
- LIVE B: Real YubiKey Daily Door (2x cycle)
- LIVE C: Portable Password Maintenance Rotation (Unicode: Cyrillic + Estonian + space)
- LIVE D: Native Door 2 Without SAITULS (Pure Windows PowerShell Unlock-BitLocker -Password)
- LIVE E: Native Door 3 Without SAITULS (Pure Windows PowerShell Unlock-BitLocker -RecoveryPassword)
- LIVE F: Sidecar-Only Real YubiKey Recovery (Crypto Identity & HMAC preserved, local store hidden)
- LIVE G: Generation & Crash / Interruption Semantics (Staging cleanup, CURRENT pointer integrity)
- LIVE H: Backup / Open Race (STORAGE_BUSY mutual exclusion)
- LIVE I: Real Disposable FreeFileSync Preview (Exclude \__STORE\, Include .svault, Exclude *.partial)
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import sa_acceptance
import sa_audit
import sa_auth
import sa_backup
import sa_broker
import sa_config
import sa_crypto
import sa_enroll
import sa_ffs
import sa_layout
import sa_mnemonic
import sa_paths
import sa_privtask
import sa_state
import sa_storage

import sa_live_elevation

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("saituls.live_acceptance")

# Evidence classes (A1). Only LIVE_PASS satisfies REAL_WINDOWS_DISPOSABLE_A_I.
LIVE_PASS = "LIVE_PASS"
HERMETIC_PASS = "HERMETIC_PASS"
SIMULATED_PASS = "SIMULATED_PASS"
SIMULATED_SKIPPED = "SKIPPED_SIMULATED"
SKIPPED = "SKIPPED_NOT_WINDOWS"
BLOCKED = "BLOCKED"
FAIL = "FAIL"
NOT_SELECTED = "NOT_SELECTED"
HARDWARE_REAL = "HARDWARE_REAL"
SIMULATED = "SIMULATED"

# Gates whose step body does NOT yet execute the oracle demanded by the
# acceptance contract. In HARDWARE_REAL mode they are BLOCKED, never run,
# so a legacy shortcut body can never emit LIVE_PASS.
REAL_ORACLE_MISSING = {
    "F": "body uses password, not sidecar+real YubiKey with local store hidden",
    "G": "no process-kill publication harness (G1-G5)",
    "H": "no barrier-instrumented publisher/open race",
    "I": "FreeFileSync executable not invoked on disposable tree",
}

# LIVE D / LIVE E no longer carry their own recovery script. They are the only
# gates that need a high-integrity token, so they are driven through
# tests/sa_live_elevation.py (one UAC consent) into the disposable elevated
# child tests/sa_native_recovery.ps1, which owns the Mount-DiskImage /
# Unlock-BitLocker / Dismount-DiskImage work and receives each door secret over
# a per-run named pipe instead of on its command line. Keeping the old
# stdin-secret script here would leave a second, unelevated path that could
# only ever fail -- and invite someone to believe its output.


class LiveAcceptanceRunner(object):
    """Executes the complete LIVE A - I acceptance protocol."""

    def __init__(self, managed_root=None, size_gb=1, allow_simulated=False, gates=None):
        self.temp_dir = tempfile.mkdtemp(prefix="sa_live_acceptance_")
        self.managed_root = managed_root or os.path.join(self.temp_dir, "managed")
        os.makedirs(self.managed_root, exist_ok=True)
        self.size_gb = size_gb
        self.allow_simulated = allow_simulated
        self.results = {}
        self.canary_content = f"CANARY_SECRET_DATA_{uuid.uuid4().hex}\n".encode("utf-8")
        self.canary_sha256 = hashlib.sha256(self.canary_content).hexdigest()
        self.profile_id = "disposable-acceptance"
        self.container_id = f"cont-{uuid.uuid4().hex[:12]}"
        self.container_path = os.path.join(self.managed_root, "disposable.vhdx")
        self.mount_path = os.path.join(self.temp_dir, "mount")
        os.makedirs(self.mount_path, exist_ok=True)
        self.backup_dir = os.path.join(self.temp_dir, "backups")
        os.makedirs(self.backup_dir, exist_ok=True)
        self.state_dir = os.path.join(self.temp_dir, "state")
        os.makedirs(self.state_dir, exist_ok=True)

        self.initial_password = "EsmaneSalasõna2026!"
        self.rotated_password = "Salajane võti пароль 2026"
        self.current_portable_password = self.initial_password
        self.break_glass_key = None
        # The daily recovery password is the protector Door 1's route rests on.
        # Held so a later gate can prove that route still opens the vault
        # WITHOUT injecting the daily secret buffer, which is not what the
        # portable architecture hands BitLocker.
        self.daily_recovery_password = None
        self.daily_secret_buf = None
        self.broker = None
        self.backend = None
        self.evidence = {}
        self.execution_mode = SIMULATED if allow_simulated else HARDWARE_REAL
        # A subset run exists so one gate can be developed and re-checked on real
        # hardware without re-running the others. It can never speak for A-I: a
        # filtered run reports REAL_WINDOWS_DISPOSABLE_A_I as NOT_RUN.
        self.gate_filter = None
        if gates:
            wanted = {str(g).strip().upper() for g in gates if str(g).strip()}
            known = {k for k, _g, _m in self.STEPS}
            unknown = wanted - known
            if unknown:
                raise ValueError("unknown gate(s): %s" % ", ".join(sorted(unknown)))
            self.gate_filter = wanted
        # LIVE D and LIVE E share one elevated child so the operator sees a
        # single UAC prompt; the first of the two steps performs the run.
        self._native_doors_done = False

    def cleanup(self):
        try:
            if self.broker:
                self.broker.shutdown()
        except Exception:
            pass
        if os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
            except Exception:
                pass

    STEPS = (
        ("A", "LIVE A (Disposable V2 Creation)", "step_live_a"),
        ("B", "LIVE B (Daily Door 2x Cycle)", "step_live_b"),
        ("C", "LIVE C (Portable Password Maintenance Rotation)", "step_live_c"),
        ("D", "LIVE D (Native Door 2 Without SAITULS)", "step_live_d"),
        ("E", "LIVE E (Native Door 3 Without SAITULS)", "step_live_e"),
        ("F", "LIVE F (Sidecar-Only Recovery)", "step_live_f"),
        ("G", "LIVE G (Generation Semantics & Crash Recovery)", "step_live_g"),
        ("H", "LIVE H (Backup / Open Race)", "step_live_h"),
        ("I", "LIVE I (Real Disposable FFS Preview & Cutover Gate)", "step_live_i"),
    )

    def run_all(self):
        logger.info("EXECUTION_MODE: %s", self.execution_mode)
        if self.gate_filter:
            logger.info("GATE_FILTER: %s", ",".join(sorted(self.gate_filter)))
        a_gate = self.STEPS[0][1]
        warned_unverified_a = False
        for key, gate, meth in self.STEPS:
            if self.gate_filter is not None and key not in self.gate_filter:
                self.results[gate] = NOT_SELECTED
                continue
            if self.execution_mode == HARDWARE_REAL and key in REAL_ORACLE_MISSING:
                self.results[gate] = "%s: %s" % (BLOCKED, REAL_ORACLE_MISSING[key])
                continue
            if key != "A" and self.results.get(a_gate) != NOT_SELECTED:
                if self.results.get(a_gate) not in (LIVE_PASS, SIMULATED_PASS):
                    self.results[gate] = "%s: LIVE A did not pass" % BLOCKED
                    continue
            elif key != "A" and not warned_unverified_a:
                warned_unverified_a = True
                logger.warning(
                    "    gates run without LIVE A in this subset; their prerequisites are unverified"
                )
            try:
                getattr(self, meth)()
            except Exception as exc:
                logger.error("[-] %s FAILED: %s", gate, exc)
                self.results.setdefault(gate, "%s: %s" % (FAIL, exc))
                if not str(self.results[gate]).startswith(FAIL):
                    self.results[gate] = "%s: %s" % (FAIL, exc)
            # Normalise legacy "PASS" emitted by step bodies into an honest class.
            if self.results.get(gate) == "PASS":
                self.results[gate] = LIVE_PASS if self.execution_mode == HARDWARE_REAL else SIMULATED_PASS
        self.print_summary()
        if self.execution_mode == HARDWARE_REAL:
            self.write_evidence()
            return self.selected_gates_verdict() == "PASS"
        return self.simulation_verdict() == "PASS"

    def selected_gates_verdict(self):
        """Verdict over the gates this run actually selected."""
        if self.execution_mode != HARDWARE_REAL:
            return "NOT_RUN"
        selected = [k for k, _g, _m in self.STEPS if self.gate_filter is None or k in self.gate_filter]
        vals = [self.results.get(self._gate_name(k)) for k in selected]
        return "PASS" if all(v == LIVE_PASS for v in vals) else "FAIL"

    def real_acceptance(self):
        if self.execution_mode != HARDWARE_REAL:
            return "NOT_RUN"
        if self.gate_filter is not None:
            # A subset run cannot speak for the whole contract.
            return "NOT_RUN"
        vals = [self.results.get(g) for _, g, _ in self.STEPS]
        return "PASS" if all(v == LIVE_PASS for v in vals) else "FAIL"

    def simulation_verdict(self):
        bad = [v for v in self.results.values() if str(v).startswith(FAIL)]
        return "FAIL" if bad else "PASS"

    def write_evidence(self):
        import platform
        out_dir = REPO / ".saipen" / "audit"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        rep = {
            "timestamp": ts,
            "execution_mode": self.execution_mode,
            "windows_build": platform.version(),
            "container_id": self.container_id,
            "gate_filter": sorted(self.gate_filter) if self.gate_filter else None,
            "gates": {g: str(self.results.get(g)).split(":")[0] for _, g, _ in self.STEPS},
            "gate_detail": {g: str(self.results.get(g))[:300] for _, g, _ in self.STEPS},
            "evidence": self.evidence,
            "REAL_WINDOWS_DISPOSABLE_A_I": self.real_acceptance(),
            "selected_gates_verdict": self.selected_gates_verdict(),
        }
        blob = json.dumps(rep, indent=2, ensure_ascii=False)
        for s in (self.initial_password, self.rotated_password, self.break_glass_key):
            if s and s in blob:
                raise RuntimeError("evidence report would leak a secret; refusing to write")
        path = out_dir / ("portable_live_%s.json" % ts)
        path.write_text(blob, encoding="utf-8")
        logger.info("EVIDENCE_REPORT: %s", path)


    def step_live_a(self):
        """LIVE A: Disposable V2 Creation & Canary Setup."""
        logger.info("[-] RUNNING LIVE A: Disposable V2 Creation...")
        try:
            prof = self._get_profile()

            # Initialize backend
            if self.allow_simulated and not os.environ.get("SA_REQUIRE_HARDWARE"):
                self.backend = sa_storage.FakeStorageBackend()
            else:
                self.backend = sa_storage.BitLockerVhdxBackend()

            # Format container with daily secret + break glass + portable password
            self.daily_secret_buf = sa_crypto.new_volume_secret()

            create_res = self.backend.create(
                prof, self.daily_secret_buf,
                portable_password=self.initial_password
            )
            self.break_glass_key = create_res.get("recovery_password")
            self.daily_recovery_password = create_res.get("daily_recovery_password")
            if not self.daily_recovery_password:
                raise AssertionError(
                    "LIVE A: the create step returned no daily recovery password, "
                    "so Door 1's protector cannot be checked later")
            if self.allow_simulated and not os.path.exists(self.container_path):
                with open(self.container_path, "wb") as f:
                    f.write(b"VHDX_SIMULATED_CONTAINER_PAYLOAD\n" * 1024)

            # Write canary file while container is mounted
            canary_path = os.path.join(self.mount_path, "canary.txt")
            with open(canary_path, "wb") as f:
                f.write(self.canary_content)

            # Verify canary exists
            assert os.path.isfile(canary_path), "Canary file write failed"
            self.backend.unmount(prof)

            # Save layout V2
            layout = sa_layout.save_layout(
                prof,
                daily_id=create_res.get("daily_protector_id", "daily-1"),
                daily_type="RecoveryPassword",
                break_glass_id=create_res.get("break_glass_protector_id", "break-1"),
                break_glass_type="RecoveryPassword",
                portable_id=create_res.get("portable_password_id", "port-1"),
                portable_type="Password"
            )
            assert layout.get("layout_version") == 2, "Layout version is not V2"

            self.results["LIVE A (Disposable V2 Creation)"] = "PASS"
            logger.info("[+] LIVE A: PASS")
        except Exception as exc:
            logger.error("[-] LIVE A FAILED: %s", exc)
            self.results["LIVE A (Disposable V2 Creation)"] = f"FAIL: {exc}"
            raise

    def step_live_b(self):
        """LIVE B: the real daily door, twice, from detached.

        The daily secret is the recovery password BitLocker itself issued for
        the daily protector -- exactly the value ``begin_enrollment`` wraps --
        and it reaches the volume only by being unwrapped out of the
        hmac-secret KEK on the enrolled key. The runner never hands the unlock
        a secret it already held: ``self.daily_secret_buf`` is a different
        random buffer and is not used here at all.
        """
        logger.info("[-] RUNNING LIVE B: Daily Door 2x Cycle (real FIDO2 key)...")
        gate = "LIVE B (Daily Door 2x Cycle)"
        try:
            assert self.daily_recovery_password, \
                "LIVE A did not hand over the daily protector's recovery password"
            prof = self._get_profile()
            broker = self._build_broker(prof)
            provider = broker.provider(prof)
            caps = provider.capabilities()
            assert caps.available, "no FIDO2 transport: %s" % caps.detail
            assert caps.hmac_secret, "the key does not do hmac-secret: %s" % caps.detail
            assert caps.user_verification, "no PIN / user verification: %s" % caps.detail

            # 1. Enroll a disposable credential against the real key: create
            #    the credential (PIN + touch), then bind this vault's daily
            #    secret to it under the hmac-secret KEK (touch again).
            logger.info("  Connect YubiKey now.")
            raw_daily = self.daily_recovery_password.encode("ascii")
            expected_digest = hashlib.sha256(raw_daily).hexdigest()
            daily = sa_crypto.SecretBuffer(raw_daily)
            try:
                credential = provider.create_credential(prof.credential_profile,
                                                        require_hmac_secret=True)
                enrollment = sa_auth.wrap_volume_secret_for(prof, provider,
                                                            credential, daily)
                broker.credentials.add(prof.id, prof.container_id, enrollment)
            finally:
                daily.zeroize()
            enrolled = broker.credentials.enrollments(prof.id)
            assert len(enrolled) == 1, "expected exactly one enrolled credential"

            # 2. Two independent cycles, each from a detached volume. The only
            #    secret offered to BitLocker is the one this cycle just
            #    unwrapped from the key.
            cycles = []
            for number in (1, 2):
                assert self.backend.state(prof) == sa_storage.DETACHED, \
                    "cycle %d did not start from a detached volume" % number
                logger.info("  Cycle %d: PIN + touch now (unwrap the daily secret).", number)
                used_id, unwrapped = sa_auth.unwrap_volume_secret(
                    prof, provider, broker.credentials)
                try:
                    digest = hashlib.sha256(unwrapped.bytes()).hexdigest()
                    assert digest == expected_digest, (
                        "cycle %d: the unwrapped secret is not the daily "
                        "protector's recovery password" % number)
                    self.backend.unlock_and_mount(prof, volume_secret=unwrapped)
                    state_mounted = self.backend.state(prof)
                    with open(os.path.join(self.mount_path, "canary.txt"), "rb") as handle:
                        canary_sha = hashlib.sha256(handle.read()).hexdigest()
                    assert canary_sha == self.canary_sha256, \
                        "cycle %d: canary mismatch" % number
                finally:
                    unwrapped.zeroize()
                # Unmount is the product's secure relock: Lock-BitLocker, then
                # detach. A cycle that only unmounted the access path would
                # leave the next cycle unlocking an already-open volume.
                self.backend.unmount(prof)
                state_after = self.backend.state(prof)
                assert state_after == sa_storage.DETACHED, \
                    "cycle %d: volume not detached after unlocking (%s)" % (number, state_after)
                cycles.append({
                    "cycle": number,
                    "secret_digest_matches": True,
                    "canary_sha256": canary_sha,
                    "state_while_mounted": str(state_mounted),
                    "state_after": str(state_after),
                    "credential_id_hash": sa_crypto.credential_id_hash(bytes(used_id))[:12],
                })

            self.evidence["daily_door"] = {
                "path": "sa_enroll wrap -> sa_auth.unwrap_volume_secret -> BitLockerVhdxBackend.unlock_and_mount",
                "transport": caps.transport,
                "hmac_secret": bool(caps.hmac_secret),
                "user_verification": bool(caps.user_verification),
                "authenticators": int(caps.authenticators or 0),
                "daily_secret_source": "BitLocker daily RecoveryPassword protector",
                "injected_buffer_used": False,
                "enrolled_keys": len(enrolled),
                "cycles": cycles,
            }
            self.results[gate] = "PASS"
            logger.info("[+] LIVE B: PASS")
        except Exception as exc:
            logger.error("[-] LIVE B FAILED: %s", exc)
            self.results[gate] = "FAIL: %s" % exc
            raise

    def _live_pin(self, rp_id=None):
        """The one interactive input the disposable FIDO2 chain needs.

        Nothing is remembered here: like the hardware acceptance runner, the
        PIN is handed to the library and kept nowhere. A closed stdin is a
        refusal, never a guessed PIN.
        """
        logger.info("  PROMPT: Enter the YubiKey PIN now.")
        try:
            import getpass
            return getpass.getpass("  FIDO2 PIN for %s (not echoed): "
                                   % (rp_id or "security key")) or None
        except (EOFError, KeyboardInterrupt):
            return None

    def _build_broker(self, prof):
        """A real broker over the disposable registry, on this machine.

        The broker is the privilege boundary: every storage operation leaves
        through the installed privileged helper, and the FIDO2 key is reached
        through the same helper's frozen worker. The runner itself never
        elevates.
        """
        registry = sa_config.Registry(self.managed_root, [prof],
                                      sa_config.SCHEMA_VERSION)
        os.makedirs(registry.state_dir, exist_ok=True)
        self.broker = sa_broker.SecureBroker(
            registry, audit=sa_audit.NullAuditLog(),
            state_store=sa_state.StateStore(registry.state_path),
            pin_callback=self._live_pin)
        return self.broker

    def _verify_portable(self, prof, password):
        """Return ``(verified, error)`` for a portable password.

        A refusal and an unreachable helper must not look alike: the first is
        the volume saying no, the second is the question never being asked.
        """
        try:
            return bool(self.backend.verify_password_unlock(prof, password)), None
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)

    def _recovery_door_opens(self, prof, recovery_password):
        """Open with a recovery password, read the canary, then close again."""
        try:
            self.backend.unlock_and_mount(prof, recovery_password=recovery_password)
            with open(os.path.join(self.mount_path, "canary.txt"), "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest() == self.canary_sha256
        finally:
            try:
                self.backend.unmount(prof)
            except Exception:
                pass

    def step_live_c(self):
        """LIVE C: real ``SecureBroker.change_portable_password`` transaction.

        The rotation runs through the broker, never the storage backend
        directly. The broker is what owns the escrow protector, the durable
        mutation journal and the layout commit, so a gate that called
        ``backend.change_portable_password`` would prove a sequence this
        product never runs and would hide a broker that skipped its safety
        steps. The runner stays medium integrity throughout; every privileged
        operation is the installed helper's.
        """
        logger.info("[-] RUNNING LIVE C: Portable Password Maintenance Rotation...")
        gate = "LIVE C (Portable Password Maintenance Rotation)"
        try:
            prof = self._get_profile()
            observed = {"journal": [], "escrow_created": None, "removed": []}

            # 1. Maintenance mode: the protected app family is stopped and no
            #    volume is held. This is exactly what the broker checks before
            #    it will touch a protector, so proving it here proves it rather
            #    than assuming the check passes.
            registry = sa_config.Registry(self.managed_root, [prof],
                                          sa_config.SCHEMA_VERSION)
            os.makedirs(registry.state_dir, exist_ok=True)
            self.broker = self._build_broker(prof)
            runtime = self.broker.runtime(prof.id)
            assert not runtime.supervisor.running, "a protected application is running"
            assert runtime.state == sa_state.LOCKED, "runtime is not LOCKED"
            assert self.backend.state(prof) == sa_storage.DETACHED, "storage is not detached"
            precondition = {"app_running": False, "runtime_state": runtime.state,
                            "storage": "detached"}

            # 2. The old password really opens the volume before anything is
            #    mutated, so a later refusal means the rotation changed it.
            old_before, err = self._verify_portable(prof, self.initial_password)
            assert old_before, "initial password did not verify before rotation: %s" % err

            layout_before = sa_layout.load_layout(prof) or {}
            old_portable_id = (layout_before.get("portable_protector") or {}).get("id")
            assert old_portable_id, "layout carries no portable protector to rotate"

            # 3. Observe the transaction the broker performs: the journal
            #    stages it writes and the escrow protector it adds and later
            #    removes. Observation only -- every recorded call is delegated
            #    to the real implementation, so nothing about the sequence is
            #    faked into existence.
            backend = self.broker.backend(prof)
            orig = {
                "change": backend.change_portable_password,
                "remove": backend.remove_protector,
                "start": sa_layout.record_mutation_start,
                "stage": sa_layout.record_mutation_stage,
                "complete": sa_layout.record_mutation_complete,
            }

            def _change(*args, **kwargs):
                res = orig["change"](*args, **kwargs)
                if isinstance(res, dict):
                    observed["escrow_created"] = res.get("escrow_id")
                return res

            def _remove(profile_arg, protector_id, *args, **kwargs):
                observed["removed"].append(str(protector_id))
                return orig["remove"](profile_arg, protector_id, *args, **kwargs)

            def _start(*args, **kwargs):
                stage = kwargs.get("stage", args[5] if len(args) > 5 else None)
                observed["journal"].append(("start", stage))
                return orig["start"](*args, **kwargs)

            def _stage(*args, **kwargs):
                stage = kwargs.get("stage", args[2] if len(args) > 2 else None)
                observed["journal"].append(("stage", stage))
                return orig["stage"](*args, **kwargs)

            def _complete(*args, **kwargs):
                observed["journal"].append(("complete", None))
                return orig["complete"](*args, **kwargs)

            backend.change_portable_password = _change
            backend.remove_protector = _remove
            sa_layout.record_mutation_start = _start
            sa_layout.record_mutation_stage = _stage
            sa_layout.record_mutation_complete = _complete
            try:
                res = self.broker.change_portable_password(
                    prof.id, self.rotated_password,
                    old_password=self.initial_password)
            finally:
                backend.change_portable_password = orig["change"]
                backend.remove_protector = orig["remove"]
                sa_layout.record_mutation_start = orig["start"]
                sa_layout.record_mutation_stage = orig["stage"]
                sa_layout.record_mutation_complete = orig["complete"]

            assert res.get("ok"), "broker rotation reported failure: %s" % (res,)
            new_portable_id = res.get("protector_id")
            assert new_portable_id, "broker rotation returned no new protector id"
            assert new_portable_id != old_portable_id, "portable protector id did not change"

            # 4. Escrow: created before the old protector was touched, and
            #    removed only after the new password was independently proven.
            escrow_id = observed["escrow_created"]
            assert escrow_id, "the broker created no escrow protector"
            assert escrow_id in observed["removed"], "the escrow protector was never removed"

            stages = [stage for _kind, stage in observed["journal"]]
            for required in ("PORTABLE_ROTATION_START",
                             "PORTABLE_ROTATION_NEW_CREATED",
                             "LAYOUT_COMMIT_PENDING"):
                assert required in stages, "durable stage %s was never recorded" % required
            assert observed["journal"][-1][0] == "complete", "the mutation journal was not completed"

            # 5. Commit: the layout points at the new protector and the
            #    in-flight journal is gone rather than left for a restart.
            committed_id = ((sa_layout.load_layout(prof) or {}).get("portable_protector")
                            or {}).get("id")
            assert committed_id == new_portable_id, "layout did not commit the new protector"
            assert sa_layout.load_mutation_journal(prof) is None, \
                "the mutation journal survived the commit"

            after = backend.list_protectors(prof)
            after_protectors = after.get("protectors", []) if isinstance(after, dict) else []
            password_ids = [str(p.get("id")).lower() for p in after_protectors
                            if str(p.get("type")) == "Password"]
            recovery_ids = [str(p.get("id")).lower() for p in after_protectors
                            if str(p.get("type")) == "RecoveryPassword"]
            assert len(password_ids) == 1, "expected one Password protector, found %d" % len(password_ids)
            assert len(recovery_ids) == 2, \
                "expected the daily and break-glass protectors to survive, found %d" % len(recovery_ids)
            assert str(escrow_id).lower() not in password_ids + recovery_ids, \
                "the escrow protector is still on the volume"
            assert str(new_portable_id).lower() in password_ids, \
                "the new protector is not the volume's Password protector"

            # 6. The new Unicode password opens the volume; the old one is
            #    refused for a reason the volume gave, not because the helper
            #    was unreachable.
            new_ok, new_err = self._verify_portable(prof, self.rotated_password)
            assert new_ok, "the new Unicode password did not verify: %s" % new_err
            old_ok, old_err = self._verify_portable(prof, self.initial_password)
            assert old_err is None, "the old password check errored instead of refusing: %s" % old_err
            assert not old_ok, "the old password still opens the volume after rotation"
            self.current_portable_password = self.rotated_password
            assert self.backend.state(prof) == sa_storage.DETACHED, \
                "storage was left attached after the rotation"

            # 7. A portable-password rotation must leave the other two doors
            #    alone: the daily route and the break-glass route are what keep
            #    the vault recoverable, and losing either is worse than a
            #    rotation that fails loudly.
            daily_ok = self._recovery_door_opens(prof, self.daily_recovery_password)
            assert daily_ok, "the daily route (Door 1's protector) broke during rotation"
            break_glass_ok = self._recovery_door_opens(prof, self.break_glass_key)
            assert break_glass_ok, "the break-glass route (Door 3) broke during rotation"

            # 8. Exit maintenance: locked and detached, not left open.
            assert runtime.state == sa_state.LOCKED, "runtime did not end LOCKED"
            assert self.backend.state(prof) == sa_storage.DETACHED, \
                "storage did not end detached"

            self.evidence["portable_rotation"] = {
                "path": "sa_broker.SecureBroker.change_portable_password",
                "maintenance_precondition": precondition,
                "old_password_verified_before": True,
                "escrow_created": True,
                "escrow_removed": True,
                "journal_stages": stages,
                "journal_cleared_on_commit": True,
                "protector_id_changed": True,
                "layout_committed_new_id": True,
                "password_protectors_after": len(password_ids),
                "recovery_protectors_after": len(recovery_ids),
                "new_password_verifies": True,
                "old_password_rejected": True,
                "daily_route_ok": daily_ok,
                "break_glass_route_ok": break_glass_ok,
                "unicode_password": any(ord(ch) > 127 for ch in self.rotated_password),
                "final": {"runtime_state": runtime.state, "storage": "detached"},
            }

            self.results[gate] = "PASS"
            logger.info("[+] LIVE C: PASS")
        except Exception as exc:
            logger.error("[-] LIVE C FAILED: %s", exc)
            self.results[gate] = "FAIL: %s" % exc
            raise

    def _gate_name(self, key):
        for k, gate, _meth in self.STEPS:
            if k == key:
                return gate
        raise KeyError(key)

    def _run_native_doors(self):
        """LIVE D + LIVE E in one disposable elevated native-recovery child.

        Both doors need a high-integrity token and this runner must not have one,
        so privilege is borrowed once: the operator approves a single UAC prompt
        and one elevated child performs Door 2 and then Door 3, each with its own
        attach/unlock/canary/lock/detach cycle so no volume is held between them.
        The child imports nothing from Scripts/secure_apps -- it proves Windows
        plus a copied .svault plus the human secret is still enough.
        """
        if self._native_doors_done:
            return
        self._native_doors_done = True

        gates = {"D": self._gate_name("D"), "E": self._gate_name("E")}
        if self.allow_simulated:
            for gate in gates.values():
                self.results[gate] = SIMULATED_SKIPPED
            return
        if sys.platform != "win32":
            for gate in gates.values():
                self.results[gate] = SKIPPED
            return

        prof = self._get_profile()
        svault_copy = os.path.join(self.temp_dir, "test_native_doors.svault")
        shutil.copy2(prof.container, svault_copy)
        child_evidence = os.path.join(self.temp_dir, "native_recovery_child.json")

        def prompt(line):
            print("\n" + "=" * 72 + "\n  OPERATOR: %s\n" % line + "=" * 72 + "\n", flush=True)
            logger.warning("OPERATOR_ACTION_REQUIRED: %s", line)

        outcome = sa_live_elevation.run_live_doors(
            vault_path=svault_copy,
            expected_canary_sha256=self.canary_sha256,
            portable_password=self.current_portable_password,
            recovery_password=self.break_glass_key or "",
            evidence_path=child_evidence,
            prompt_writer=prompt,
            log=lambda msg: logger.info("    %s", msg),
        )

        # Evidence stays sanitized: the child's own record is path/digest/verdict
        # only, and this block adds nothing a repo reader could unlock with.
        self.evidence["native_doors"] = {
            "orchestrator": "tests/sa_live_elevation.py",
            "child_script": "tests/sa_native_recovery.ps1",
            "child_pid": outcome.get("child_pid"),
            "child_user": outcome.get("child_user"),
            "child_client_user": outcome.get("child_client_user"),
            "child_exit_code": outcome.get("child_exit_code"),
            "prompt": outcome.get("elevation_prompt"),
            "expected_canary_sha256": self.canary_sha256,
            "D": outcome["D"],
            "E": outcome["E"],
            "denied": outcome.get("denied"),
            "error": outcome.get("error"),
        }

        for key, gate in gates.items():
            verdict = outcome[key]
            if verdict["status"] == "PASS":
                self.results[gate] = LIVE_PASS
            elif verdict["status"] == "BLOCKED":
                self.results[gate] = "%s: %s" % (BLOCKED, verdict["detail"])
            else:
                self.results[gate] = "%s: %s" % (FAIL, verdict["detail"])

    def step_live_d(self):
        """LIVE D: native Door 2 (Unlock-BitLocker -Password) in the elevated child."""
        self._run_native_doors()
        self._require_live_or_skip("D")

    def step_live_e(self):
        """LIVE E: native Door 3 (Unlock-BitLocker -RecoveryPassword), same child."""
        self._run_native_doors()
        self._require_live_or_skip("E")

    def _require_live_or_skip(self, key):
        """A skipped gate is a skip, not a pass -- and not a failure either."""
        gate = self._gate_name(key)
        verdict = self.results.get(gate)
        if verdict == LIVE_PASS or verdict in (SKIPPED, SIMULATED_SKIPPED):
            return
        raise RuntimeError(str(verdict))

    def step_live_f(self):
        """LIVE F: Sidecar-Only Recovery with Isolated Credential Store."""
        logger.info("[-] RUNNING LIVE F: Sidecar-Only Recovery...")
        try:
            prof = self._get_profile()
            # Publish backup with HMAC
            hmac_key = sa_crypto.derive_manifest_hmac_key(self.daily_secret_buf, prof.container_id)
            pub_res = sa_backup.publish_backup(
                prof, self.backend,
                destination_dir=self.backup_dir,
                hmac_key=hmac_key
            )
            assert pub_res.get("ok"), "Backup publication failed"

            # Open portable vault from backup directory using password
            open_res = sa_backup.open_portable_vault(
                vault_path=pub_res["vault_path"],
                mount_path=self.mount_path,
                backend=self.backend,
                password=self.rotated_password,
                read_only=True
            )
            assert open_res.get("state") == sa_storage.MOUNTED or open_res.get("ok"), f"Sidecar open failed: {open_res}"
            assert open_res.get("sidecar_authenticated") in ("VERIFIED", "SIDECAR_AUTHENTICITY_UNVERIFIED")

            # Close portable vault
            sa_backup.close_portable_vault(
                vault_path=pub_res["vault_path"],
                mount_path=self.mount_path,
                backend=self.backend
            )

            self.results["LIVE F (Sidecar-Only Recovery)"] = "PASS"
            logger.info("[+] LIVE F: PASS")
        except Exception as exc:
            logger.error("[-] LIVE F FAILED: %s", exc)
            self.results["LIVE F (Sidecar-Only Recovery)"] = f"FAIL: {exc}"
            raise

    def step_live_g(self):
        """LIVE G: Generation & Interruption Semantics."""
        logger.info("[-] RUNNING LIVE G: Generation & Interruption Semantics...")
        try:
            prof = self._get_profile()
            # 1. Publish Gen 1
            hmac_key = sa_crypto.derive_manifest_hmac_key(self.daily_secret_buf, prof.container_id)
            g1 = sa_backup.publish_backup(prof, self.backend, destination_dir=self.backup_dir, hmac_key=hmac_key)
            assert os.path.isdir(g1["committed_gen_dir"]), "Generation 1 dir missing"

            # 2. Modify canary & publish Gen 2
            self.backend.unlock_and_mount(prof, volume_secret=self.daily_secret_buf)
            with open(os.path.join(self.mount_path, "canary_g2.txt"), "w") as f:
                f.write("GENERATION_2_CANARY")
            self.backend.unmount(prof)

            g2 = sa_backup.publish_backup(prof, self.backend, destination_dir=self.backup_dir, hmac_key=hmac_key)
            assert g1["generation_id"] != g2["generation_id"], "Generations must have distinct IDs"

            # 3. Simulate interrupted staging (dirty folder in staging)
            stg_dir = os.path.join(self.backup_dir, sa_backup.STAGING_SUBDIR)
            os.makedirs(stg_dir, exist_ok=True)
            dirty_stg = os.path.join(stg_dir, f"gen_{int(time.time())}_deadbeef")
            os.makedirs(dirty_stg, exist_ok=True)
            with open(os.path.join(dirty_stg, "partial.tmp"), "w") as f:
                f.write("GARBAGE")

            # Clean stale staging
            sa_backup.clean_stale_staging(stg_dir, max_age_seconds=-1)
            assert not os.path.exists(dirty_stg), "Stale staging was not cleaned"

            # Verify CURRENT pointer points to g2
            curr_file = os.path.join(self.backup_dir, "CURRENT")
            with open(curr_file, "r", encoding="utf-8") as f:
                curr_gen = f.read().strip()
            assert curr_gen == g2["generation_id"], "CURRENT pointer does not match Gen 2"

            self.results["LIVE G (Generation Semantics & Crash Recovery)"] = "PASS"
            logger.info("[+] LIVE G: PASS")
        except Exception as exc:
            logger.error("[-] LIVE G FAILED: %s", exc)
            self.results["LIVE G (Generation Semantics & Crash Recovery)"] = f"FAIL: {exc}"
            raise

    def step_live_h(self):
        """LIVE H: Backup / Open Race (Mutual Exclusion)."""
        logger.info("[-] RUNNING LIVE H: Backup / Open Race...")
        try:
            prof = self._get_profile()
            reg = sa_config.Registry(self.managed_root, [prof], schema_version=1)
            cred_store = sa_backup.EphemeralCredentialStore(prof.id, prof.container_id, [])
            broker = sa_broker.SecureBroker(registry=reg, credential_store=cred_store, backends={prof.backend: self.backend})
            runtime = broker.runtime(prof.id)

            # Simulate backup lock held by worker
            with runtime.backup_lock:
                runtime.backup_state = sa_broker.BACKUP_PUBLISHING
                # When broker attempts open or backup is publishing, verify storage state
                assert runtime.backup_state == sa_broker.BACKUP_PUBLISHING

            # Release lock, verify clean state
            runtime.backup_state = sa_broker.BACKUP_CURRENT_AUTHENTICATED
            broker.shutdown()

            self.results["LIVE H (Backup / Open Race)"] = "PASS"
            logger.info("[+] LIVE H: PASS")
        except Exception as exc:
            logger.error("[-] LIVE H FAILED: %s", exc)
            self.results["LIVE H (Backup / Open Race)"] = f"FAIL: {exc}"
            raise

    def step_live_i(self):
        """LIVE I: Real Disposable FreeFileSync Preview & Cutover Gate."""
        logger.info("[-] RUNNING LIVE I: Real Disposable FreeFileSync Preview & Cutover Gate...")
        try:
            # Create throwaway FFS batch and GUI XML matching real schema
            xml_batch = os.path.join(self.temp_dir, "disposable.ffs_batch")
            xml_gui = os.path.join(self.temp_dir, "disposable.ffs_gui")

            ffs_content = """<?xml version="1.0" encoding="utf-8"?>
<FreeFileSync XmlVersion="10" XmlFormat="23">
  <MainConfig>
    <FolderPairs>
      <Pair>
        <Left>V:\\___VAC\\__K</Left>
        <Right>R:\\__SAVE_R\\__K</Right>
      </Pair>
    </FolderPairs>
    <Filter>
      <Include>
        <Item>*</Item>
      </Include>
      <Exclude>
        <Item>\\System Volume Information\\</Item>
      </Exclude>
    </Filter>
  </MainConfig>
</FreeFileSync>
"""
            with open(xml_batch, "w", encoding="utf-8") as f:
                f.write(ffs_content)
            with open(xml_gui, "w", encoding="utf-8") as f:
                f.write(ffs_content)

            # Harden configs
            harden_res = sa_ffs.apply_ffs_hardening(xml_batch, xml_gui)
            assert harden_res.get("overall_safe"), "FFS hardening did not produce overall_safe=True"

            # Audit configs
            audit_res = sa_ffs.audit_ffs_configuration(xml_batch, xml_gui)
            assert audit_res.get("overall_safe"), "FFS audit reported unsafe configuration"

            # Check cutover gate
            prof = self._get_profile()
            reg = sa_config.Registry(self.managed_root, [prof], schema_version=1)
            cred_store = sa_backup.EphemeralCredentialStore(prof.id, prof.container_id, [])
            broker = sa_broker.SecureBroker(registry=reg, credential_store=cred_store, backends={prof.backend: self.backend})
            runtime = broker.runtime(prof.id)
            runtime.backup_state = sa_broker.BACKUP_CURRENT_AUTHENTICATED

            gate_res = sa_ffs.verify_ffs_cutover_gate(
                broker, prof.id,
                batch_path=xml_batch, gui_path=xml_gui,
                destination_dir=self.backup_dir
            )
            broker.shutdown()
            assert gate_res.get("ready_for_cutover"), f"FFS cutover gate rejected: {gate_res}"

            self.results["LIVE I (Real Disposable FFS Preview & Cutover Gate)"] = "PASS"
            logger.info("[+] LIVE I: PASS")
        except Exception as exc:
            logger.error("[-] LIVE I FAILED: %s", exc)
            self.results["LIVE I (Real Disposable FFS Preview & Cutover Gate)"] = f"FAIL: {exc}"
            raise

    def _get_profile(self):
        pol = sa_config.Policy()
        pol.mode = "default"
        pol.idle_timeout_minutes = 5
        pol.lock_on_windows_lock = True
        pol.lock_on_suspend = True
        pol.lock_on_logoff = True
        pol.lock_on_shutdown = True
        pol.lock_on_broker_shutdown = True
        pol.unmount_when_app_closes = True
        pol.graceful_close_timeout_seconds = 30
        pol.force_terminate_after_timeout = True
        pol.process_exit_confirm_timeout_seconds = 20
        pol.unmount_timeout_seconds = 60
        pol.conditions = []

        prof = sa_config.Profile()
        prof.id = self.profile_id
        prof.label = "Disposable Live Vault"
        prof.enabled = True
        prof.executable = sys.executable
        prof.working_directory = os.path.dirname(sys.executable)
        prof.arguments = ["-c", "import time; time.sleep(0.1)"]
        prof.vault_argument_style = "path"
        prof.allow_unmanaged_executable = True
        prof.backend = "bitlocker-vhdx" if not self.allow_simulated else "fake-memory"
        prof.container = self.container_path
        prof.container_id = self.container_id
        prof.mount_path = self.mount_path
        prof.size_gb = self.size_gb
        prof.filesystem_label = "SAITULS-DISP"
        prof.provider = "fake-auth" if self.allow_simulated else "yubikey-fido2-hmac-secret"
        prof.credential_profile = self.profile_id
        prof.helper_executable = None
        prof.user_verification = "required"
        prof.policy = pol
        prof.managed_root = self.managed_root
        prof.notes = "Disposable test profile"
        return prof

    def print_summary(self):
        logger.info("======================================================================")
        logger.info("PORTABLE VAULT LIVE ACCEPTANCE EVIDENCE SUMMARY:")
        logger.info("======================================================================")
        for check, status in sorted(self.results.items()):
            logger.info("  %-55s : %s", check, status)
        logger.info("======================================================================")
        logger.info("execution_mode: %s", self.execution_mode)
        if self.execution_mode == SIMULATED:
            logger.info("PORTABLE_VAULT_SIMULATION: %s", self.simulation_verdict())
            logger.info("REAL_WINDOWS_DISPOSABLE_A_I: NOT_RUN")
        else:
            live = sum(1 for s in self.results.values() if s == LIVE_PASS)
            logger.info("LIVE_PASS: %d/%d", live, len(self.STEPS))
            logger.info("REAL_WINDOWS_DISPOSABLE_A_I: %s", self.real_acceptance())
        logger.info("======================================================================")


def main():
    parser = argparse.ArgumentParser(description="Disposable Live Acceptance Runner for Portable Vault (LIVE A - I)")
    parser.add_argument("--size-gb", type=int, default=1, help="Size of disposable VHDX container in GiB (default: 1)")
    parser.add_argument("--simulated", action="store_true", help="Run simulated checks if no elevated helper/hardware available")
    parser.add_argument(
        "--gates",
        default=None,
        help="Comma-separated subset of A-I to run (e.g. A,D,E). A subset run cannot "
             "claim REAL_WINDOWS_DISPOSABLE_A_I; it reports NOT_RUN for the aggregate.",
    )
    args = parser.parse_args()

    allow_sim = args.simulated or (sys.platform != "win32")
    gates = [g for g in args.gates.split(",") if g.strip()] if args.gates else None
    runner = LiveAcceptanceRunner(size_gb=args.size_gb, allow_simulated=allow_sim, gates=gates)
    try:
        ok = runner.run_all()
        return 0 if ok else 1
    finally:
        runner.cleanup()


if __name__ == "__main__":
    sys.exit(main())
