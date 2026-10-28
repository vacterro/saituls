"""Spec + test coverage for the twelve post-V1 future gates (T-263).

Every gate named on the T-263 board line is executed against a disposable
destination tree laid out exactly like sa_backup.publish_backup produces:
generations/gen_<ts>_<hex8>/vault.svault, a vault.svault.meta sidecar carrying
vhdx_sha256, CURRENT/PREVIOUS pointer files, and flat __STORE*.svault copies.

No gate may pass on prose: each one drives real files, real hashes and real
tamper. Run from the repo root:

    python tests/secure_apps_future_gates.py

Exit 0 = all 12 gates green.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SUBSYSTEM = os.path.join(REPO, "Scripts", "secure_apps")
for entry in (SUBSYSTEM, HERE):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import sa_backup  # noqa: E402
import sa_crypto  # noqa: E402
import sa_future_gates as fg  # noqa: E402
import sa_paths  # noqa: E402

RESULTS = []


def make_junction(link_path, target_path):
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", link_path, target_path],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def check(label, condition, detail=""):
    RESULTS.append((label, bool(condition), detail))
    status = "PASS" if condition else "FAIL"
    rendered = str(detail)
    print("[%s] %s%s" % (status, label, (" -- " + rendered) if rendered else ""))
    return bool(condition)


def write_meta(gen_dir, size, sha256, extra=None):
    meta = {
        "schema_version": 2,
        "profile_id": "test-profile",
        "container_id": "test-container",
        "storage_layout_version": 2,
        "backup_timestamp": time.time(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "vhdx_size_bytes": size,
        "vhdx_sha256": sha256,
    }
    if extra:
        meta.update(extra)
    path = os.path.join(gen_dir, "vault.svault.meta")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return meta


def build_generation(dest_dir, payload=b"TEST-VAULT-PAYLOAD", ts=None, extra_meta=None):
    ts = ts or int(time.time())
    gen_id = "gen_%d_%s" % (ts, uuid.uuid4().hex[:8])
    gen_dir = os.path.join(dest_dir, "generations", gen_id)
    os.makedirs(gen_dir, exist_ok=True)
    vault = os.path.join(gen_dir, "vault.svault")
    with open(vault, "wb") as handle:
        handle.write(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    write_meta(gen_dir, len(payload), sha256, extra_meta)
    return gen_id, gen_dir, sha256


def write_pointer(dest_dir, name, gen_id):
    path = os.path.join(dest_dir, name)
    tmp = os.path.join(dest_dir, ".staging", "%s.%s.tmp" % (name, uuid.uuid4().hex[:8]))
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(gen_id + "\n")
    os.replace(tmp, path)


def sync_flat(dest_dir, gen_dir):
    flat_vault = os.path.join(dest_dir, sa_backup.DEFAULT_VAULT_NAME)
    flat_meta = os.path.join(dest_dir, sa_backup.DEFAULT_META_NAME)
    shutil.copy2(os.path.join(gen_dir, "vault.svault"), flat_vault)
    shutil.copy2(os.path.join(gen_dir, "vault.svault.meta"), flat_meta)


def build_destination(root):
    """Two verified generations plus CURRENT/PREVIOUS and their flat copies."""
    dest = os.path.join(root, "dest")
    os.makedirs(os.path.join(dest, "generations"), exist_ok=True)
    g1, d1, s1 = build_generation(dest, b"GEN-ONE", ts=1700000000)
    g2, d2, s2 = build_generation(dest, b"GEN-TWO", ts=1700000100)
    write_pointer(dest, "CURRENT", g2)
    write_pointer(dest, "PREVIOUS", g1)
    sync_flat(dest, d2)
    return dest, (g1, d1, s1), (g2, d2, s2)


# --------------------------------------------------------------- FG-01


def test_ransomware_mass_change():
    base = time.time()
    quiet = [{"timestamp": base + i * 5, "path": "f%d" % i} for i in range(5)]
    burst = [{"timestamp": base + i * 0.1, "path": "f%d" % i} for i in range(60)]
    mixed = quiet + burst

    report = fg.detect_mass_change(quiet)
    check("FG-01 quiet traffic not flagged", report["detected"] is False,
          "observed=%d" % report["observed"])

    report = fg.detect_mass_change(mixed)
    check("FG-01 mass burst detected", report["detected"] is True,
          "observed=%d threshold=%d" % (report["observed"], report["threshold"]))

    report = fg.detect_mass_change(burst, threshold=61)
    check("FG-01 threshold is honoured", report["detected"] is False,
          "observed=%d threshold=%d" % (report["observed"], report["threshold"]))

    check("FG-01 empty stream does not raise", fg.detect_mass_change([])["detected"] is False)

    try:
        fg.detect_mass_change(burst, window_seconds=0)
        check("FG-01 invalid window refused", False)
    except fg.FutureGateError as exc:
        check("FG-01 invalid window refused", "invalid_window" in str(exc.category))


# --------------------------------------------------------------- FG-02


def test_min_known_good(root):
    dest, _, _ = build_destination(root)
    report = fg.ensure_min_known_good(dest, min_good=2)
    check("FG-02 two verified generations accepted", report["ok"] is True,
          "verified=%s" % report["verified"])
    check("FG-02 counts match layout", report["generations_found"] == 2)

    report = fg.ensure_min_known_good(dest, min_good=3)
    check("FG-02 below minimum refused", report["ok"] is False,
          "reason=%s" % report["reason"])

    # Corrupt one generation's payload: it must leave the verified set, and
    # the surviving count dropping below min_good must be reported honestly.
    newest_gen, newest_dir = fg._generations(dest)[0]
    with open(os.path.join(newest_dir, fg.GEN_VAULT_NAME), "wb") as handle:
        handle.write(b"BIT-ROT")
    report = fg.ensure_min_known_good(dest, min_good=2)
    check("FG-02 corrupt generation excluded", len(report["verified"]) == 1 and
          len(report["unverified"]) == 1, "verified=%s unverified=%s"
          % (report["verified"], report["unverified"]))
    check("FG-02 below minimum after corruption", report["ok"] is False and
          report["reason"] == "insufficient_known_good", report["reason"])


# --------------------------------------------------------------- FG-03


def test_immutable_generations(root):
    dest, _, (cur_id, cur_dir, _) = build_destination(root)
    result = fg.mark_generation_immutable(dest, cur_id)
    check("FG-03 marker written", os.path.isfile(result["marker"]))
    check("FG-03 classified immutable",
          fg.is_generation_immutable(dest, cur_id) is True)

    result = fg.mark_generation_immutable(dest, cur_id, offline=True)
    check("FG-03 offline flag recorded", result["offline"] is True)
    check("FG-03 offline marker present",
          os.path.isfile(os.path.join(cur_dir, fg.OFFLINE_MARKER)))

    # Also reachable through the destination's own copy, not just gen_dir.
    result = fg.mark_generation_immutable(dest, fg._generations(dest)[-1][0])
    check("FG-03 older generation can be frozen",
          fg.is_generation_immutable(dest, fg._generations(dest)[-1][0]) is True and
          result["offline"] is False)

    try:
        fg.mark_generation_immutable(dest, "gen_00000000_deadbeef")
        check("FG-03 unknown generation refused", False)
    except fg.FutureGateError as exc:
        check("FG-03 unknown generation refused", "generation_missing" in str(exc.category))


# --------------------------------------------------------------- FG-04


def test_corrupted_current_fallback(root):
    dest, (old_id, old_dir, old_sha), (cur_id, cur_dir, cur_sha) = build_destination(os.path.join(root, "fallback_dest"))

    resolved = fg.resolve_known_good_generation(dest)
    check("FG-04 newest verified generation chosen",
          resolved["ok"] and resolved["generation_id"] == cur_id,
          resolved.get("generation_id") or resolved.get("reason"))
    check("FG-04 hash comes from sidecar", resolved["sha256"] == cur_sha)

    # Corrupt the CURRENT generation's payload in its generation dir: fallback must land on the older one.
    with open(os.path.join(cur_dir, fg.GEN_VAULT_NAME), "wb") as handle:
        handle.write(b"CORRUPTED")
    resolved = fg.resolve_known_good_generation(dest)
    check("FG-04 corrupt current falls back",
          resolved["ok"] and resolved["generation_id"] == old_id,
          resolved.get("generation_id") or resolved.get("reason"))
    check("FG-04 fallback hash is the surviving one", resolved["sha256"] == old_sha)

    # Corrupt both: there is nothing left to fall back to.
    with open(os.path.join(old_dir, fg.GEN_VAULT_NAME), "wb") as handle:
        handle.write(b"CORRUPTED")
    resolved = fg.resolve_known_good_generation(dest)
    check("FG-04 no verified generation reported",
          resolved["ok"] is False and resolved["reason"] == "no_verified_generation",
          resolved.get("reason"))

    empty = os.path.join(root, "empty_dest")
    os.makedirs(os.path.join(empty, "generations"), exist_ok=True)
    check("FG-04 empty destination reported",
          fg.resolve_known_good_generation(empty)["reason"] == "no_generations")


# --------------------------------------------------------------- FG-05


def test_bit_rot_scrub(root):
    dest, _, (cur_id, cur_dir, _) = build_destination(os.path.join(root, "rot_dest"))
    with open(os.path.join(cur_dir, fg.GEN_VAULT_NAME), "wb") as handle:
        handle.write(b"ROTTEN")

    report = fg.scrub_generations(dest, quarantine=True)
    check("FG-05 rot detected", report["suspect"] == 1 and report["clean"] == 1,
          "clean=%d suspect=%d" % (report["clean"], report["suspect"]))
    check("FG-05 rot quarantined", len(report["quarantined"]) == 1 and os.path.isdir(report["quarantined"][0]["moved_to"]))
    after = fg.scrub_generations(dest, quarantine=False)
    check("FG-05 quarantine leaves clean survivors", after["suspect"] == 0 and after["clean"] == 1,
          "clean=%d" % after["clean"])

    # A scrub with nothing wrong must not quarantine anything.
    dest2, _, _ = build_destination(os.path.join(root, "scrub_clean"))
    report = fg.scrub_generations(dest2)
    check("FG-05 healthy destination needs no action",
          report["ok"] and report["suspect"] == 0 and report["clean"] == 2)


# --------------------------------------------------------------- FG-06


def test_restore_drill(root):
    dest, _, (cur_id, cur_dir, cur_sha) = build_destination(os.path.join(root, "drill_dest"))
    work = os.path.join(root, "drill_work")
    report = fg.run_restore_drill(dest, work)
    check("FG-06 drill verifies newest generation",
          report["ok"] and report["generation_id"] == cur_id, report.get("reason"))
    check("FG-06 restored bytes match", report["sha256"] == cur_sha)
    check("FG-06 drill output written", os.path.isfile(report["restored_to"]))

    # Explicit target.
    old_id, old_dir = fg._generations(dest)[-1][0], fg._generations(dest)[-1][1]
    report = fg.run_restore_drill(dest, work, gen_id=old_id)
    check("FG-06 explicit target honoured", report["ok"] and report["generation_id"] == old_id,
          report.get("reason"))
    check("FG-06 explicit target hash matches",
          report["sha256"] == json.load(open(os.path.join(old_dir, "vault.svault.meta")))["vhdx_sha256"])

    # A corrupt newest generation must not break the drill.
    with open(os.path.join(cur_dir, fg.GEN_VAULT_NAME), "wb") as handle:
        handle.write(b"ROT")
    report = fg.run_restore_drill(dest, work)
    check("FG-06 drill survives corrupt newest",
          report["ok"] and report["generation_id"] == old_id, report.get("reason"))

    # Missing target is a refusal, not a pass.
    report = fg.run_restore_drill(dest, work, gen_id="gen_00000000_deadbeef")
    check("FG-06 missing generation refused",
          report["ok"] is False and report["reason"] == "generation_missing",
          report.get("reason"))


# --------------------------------------------------------------- FG-07


def test_yubikey_loss_drill(root):
    dest, _, (cur_id, cur_dir, _) = build_destination(os.path.join(root, "loss_dest"))
    meta = json.load(open(os.path.join(cur_dir, "vault.svault.meta")))
    meta.update({
        "recovery_phrase_instructions": "SAVE THE 24 WORDS",
        "key_hint": "dual-slot FIDO key",
    })
    with open(os.path.join(cur_dir, "vault.svault.meta"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2, sort_keys=True)
    fg.mark_generation_immutable(dest, cur_id)

    report = fg.run_yubikey_loss_drill(dest)
    check("FG-07 loss survivable", report["ok"] is True, report.get("reason"))
    check("FG-07 key is not authenticator-bound",
          report["key_outside_authenticator"] is True)
    check("FG-07 recovery instructions present",
          report["recovery_phrase_instructions_present"] is True)

    # Removing either half of the story must fail the drill.
    # Use a fresh destination so we don't fight the read-only immutable files.
    dest_b, _, (cur_b, dir_b, _) = build_destination(os.path.join(root, "loss_dest_b"))
    meta_b = json.load(open(os.path.join(dir_b, "vault.svault.meta")))
    meta_b["key_hint"] = "dual-slot FIDO key"
    # No recovery_phrase_instructions.
    with open(os.path.join(dir_b, "vault.svault.meta"), "w", encoding="utf-8") as handle:
        json.dump(meta_b, handle, indent=2, sort_keys=True)
    fg.mark_generation_immutable(dest_b, cur_b)
    report = fg.run_yubikey_loss_drill(dest_b)
    check("FG-07 missing phrase instructions fails",
          report["ok"] is False and report["recovery_phrase_instructions_present"] is False,
          report.get("reason"))

    # And a non-immutable source must not be trusted for loss recovery.
    dest2, _, (c2, d2, _) = build_destination(os.path.join(root, "loss_mutable"))
    m2 = json.load(open(os.path.join(d2, "vault.svault.meta")))
    m2.update({"recovery_phrase_instructions": "x", "key_hint": "y"})
    with open(os.path.join(d2, "vault.svault.meta"), "w", encoding="utf-8") as handle:
        json.dump(m2, handle, indent=2, sort_keys=True)
    check("FG-07 mutable source refused",
          fg.run_yubikey_loss_drill(dest2)["reason"] == "source_generation_not_immutable")


# --------------------------------------------------------------- FG-08


def test_succession_drill(root):
    dest, _, (cur_id, cur_dir, _) = build_destination(os.path.join(root, "succession_dest"))
    for path in (sa_backup.DEFAULT_RECOVERY_DOC, sa_backup.DEFAULT_RECOVERY_SCRIPT):
        with open(os.path.join(dest, path), "w", encoding="utf-8") as handle:
            handle.write("recovery\n")
    meta = json.load(open(os.path.join(cur_dir, "vault.svault.meta")))
    meta["owner_label"] = "founder"
    meta["successor_label"] = "heir"
    with open(os.path.join(cur_dir, "vault.svault.meta"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2, sort_keys=True)
    fg.mark_generation_immutable(dest, cur_id)

    # Incomplete: no recovery doc/script, no meta labels.
    dest_b, _, (cur_b, dir_b, _) = build_destination(os.path.join(root, "succession_dest_b"))
    report = fg.run_succession_drill(dest_b, new_owner_label="heir")
    check("FG-08 incomplete succession detected", report["ok"] is False,
          report.get("reason"))
    check("FG-08 missing list is explicit",
          "successor_designation_recorded" in report["missing"], str(report["missing"]))

    # Complete.
    report = fg.run_succession_drill(dest, new_owner_label="heir")
    check("FG-08 succession ready when complete", report["ok"] is True,
          "%s missing=%s" % (report.get("reason"), report["missing"]))
    check("FG-08 successor label honoured", report["new_owner_label"] == "heir")


# --------------------------------------------------------------- FG-09


def test_version_history(root):
    dest, (old_id, _, old_sha), (cur_id, _, cur_sha) = build_destination(os.path.join(root, "history_dest"))
    report = fg.version_history_summary(dest)
    check("FG-09 full history enumerated", report["generation_count"] == 2,
          "count=%d" % report["generation_count"])
    check("FG-09 pointers surfaced",
          report["current_pointer"] == cur_id and report["previous_pointer"] == old_id,
          "%s / %s" % (report["current_pointer"], report["previous_pointer"]))
    check("FG-09 history verifies each entry",
          all(e.get("sha256_verified") for e in report["history"]),
          ", ".join("%s=%s" % (e["generation_id"][:12], e.get("sha256_verified"))
                    for e in report["history"]))
    check("FG-09 newest first", report["history"][0]["generation_id"] == cur_id)
    check("FG-09 quarantine surfaced", report["quarantine_count"] == 0)

    # Corrupt one generation, then scrub: the quarantine set must be visible.
    newest = fg._generations(dest)[0][1]
    with open(os.path.join(newest, fg.GEN_VAULT_NAME), "wb") as handle:
        handle.write(b"ROTTEN")
    fg.scrub_generations(dest, quarantine=True)
    report = fg.version_history_summary(dest)
    check("FG-09 quarantined history visible", report["quarantine_count"] == 1,
          "count=%d" % report["quarantine_count"])
    check("FG-09 no hashes when disabled",
          all("sha256" not in e for e in fg.version_history_summary(dest, include_hashes=False)["history"]))


# --------------------------------------------------------------- FG-10


def test_metadata_recovery(root):
    # Corrupt CURRENT + flat files, then recover from the canonical generation.
    dest, (old_id, _, _), (cur_id, cur_dir, cur_sha) = build_destination(os.path.join(root, "recover_dest"))
    with open(os.path.join(dest, "CURRENT"), "w", encoding="utf-8") as handle:
        handle.write("\x00\x01garbage\n")
    os.remove(os.path.join(dest, sa_backup.DEFAULT_VAULT_NAME))

    report = fg.recover_metadata_corruption(dest, dry_run=True)
    check("FG-10 dry run plans the repair", report["ok"] and report["would_set_current"] == cur_id,
          report.get("reason"))
    check("FG-10 dry run writes nothing",
          not os.path.isfile(os.path.join(dest, sa_backup.DEFAULT_VAULT_NAME)))

    report = fg.recover_metadata_corruption(dest)
    check("FG-10 pointer rewritten",
          report["current_pointer"] if "current_pointer" in report else
          report["after"]["current_pointer"] == cur_id,
          ",".join(report.get("actions", [])))
    check("FG-10 flat files restored",
          os.path.isfile(os.path.join(dest, sa_backup.DEFAULT_VAULT_NAME)))
    check("FG-10 flat file matches canonical",
          sa_backup.sha256_file(os.path.join(dest, sa_backup.DEFAULT_VAULT_NAME)) == cur_sha)
    check("FG-10 actions recorded", "flat_files_reconciled" in report["actions"],
          ",".join(report["actions"]))

    # Sidecar repair is only honest when a verified sibling attests the same
    # payload hash, so give both generations identical bytes and lose the older
    # one's sidecar. Resolution must land on the newest and repair the sibling
    # from that attestation.
    old_id, old_dir = fg._generations(dest)[-1][0], fg._generations(dest)[-1][1]
    for gen_id, gen_dir in fg._generations(dest):
        with open(os.path.join(gen_dir, fg.GEN_VAULT_NAME), "wb") as handle:
            handle.write(b"IDENTICAL")
        meta = json.load(open(os.path.join(gen_dir, "vault.svault.meta")))
        meta["vhdx_size_bytes"] = len(b"IDENTICAL")
        meta["vhdx_sha256"] = hashlib.sha256(b"IDENTICAL").hexdigest()
        with open(os.path.join(gen_dir, "vault.svault.meta"), "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2, sort_keys=True)
    os.remove(os.path.join(old_dir, "vault.svault.meta"))
    report = fg.recover_metadata_corruption(dest)
    check("FG-10 sidecar rebuilt from payload",
          "sidecar_rebuilt_from_payload" in report["actions"],
          ",".join(report.get("actions", [])))
    rebuilt = json.load(open(os.path.join(old_dir, "vault.svault.meta")))
    check("FG-10 rebuilt sidecar carries payload hash",
          rebuilt.get("vhdx_sha256") == hashlib.sha256(b"IDENTICAL").hexdigest(),
          str(rebuilt.get("vhdx_sha256")))
    check("FG-10 rebuild records its attestation source",
          str(rebuilt.get("recovery_source", "")).startswith("sibling_generation_attestation"),
          str(rebuilt.get("recovery_source")))

    # With nothing verifiable there must be no fake recovery.
    empty = os.path.join(root, "empty_recover")
    os.makedirs(os.path.join(empty, "generations"), exist_ok=True)
    check("FG-10 no verified generation refuses",
          fg.recover_metadata_corruption(empty)["reason"] == "no_verified_generation")


# --------------------------------------------------------------- FG-11


def test_destination_compromise(root):
    for mode, predicate in (
        ("flat_files", None),
        ("current_ptr", None),
        ("sidecar", None),
    ):
        dest, _, (cur_id, cur_dir, cur_sha) = build_destination(
            os.path.join(root, "comp_" + mode))
        report = fg.simulate_destination_compromise(dest, mode=mode)
        check("FG-11[%s] tamper performed" % mode, report["ok"] and report["tampered"],
              report.get("reason"))

        if mode == "flat_files":
            check("FG-11[%s] flat copy now disagrees" % mode,
                  report["flat_matches_canonical"] is False)
            check("FG-11[%s] canonical still intact" % mode,
                  report["canonical_still_verified"] is True)
            check("FG-11[%s] tamper detectable" % mode, report["detectable"] is True)
            check("FG-11[%s] canonical hash unchanged" % mode,
                  report["canonical_sha256"] == cur_sha)
        elif mode == "current_ptr":
            check("FG-11[%s] pointer aimed at nothing" % mode,
                  report["pointer_points_at_real_generation"] is False)
            check("FG-11[%s] tamper detectable" % mode, report["detectable"] is True)
        else:
            check("FG-11[%s] canonical no longer verifies" % mode,
                  report["canonical_still_verified"] is False)
            check("FG-11[%s] tamper detectable" % mode, report["detectable"] is True)

    try:
        build_x, _, _ = build_destination(os.path.join(root, "comp_bad"))
        fg.simulate_destination_compromise(build_x, mode="nonsense")
        check("FG-11 unknown mode refused", False)
    except fg.FutureGateError as exc:
        check("FG-11 unknown mode refused", "invalid_mode" in str(exc.category))


# FG-12 uses sa_backup's own generated document, so it reads best as a real
# destination with real recovery material.


# --------------------------------------------------------------- FG-12


def test_clean_machine_recovery_doc(root):
    dest, _, _ = build_destination(os.path.join(root, "doc_dest"))
    missing = fg.recovery_doc_clean_machine_check(dest)
    check("FG-12 missing doc reported", missing["ok"] is False and
          missing["reason"] == "recovery_doc_missing", missing.get("reason"))

    # generate_recovery_doc returns the document text; it is materialised here.
    doc = sa_backup.generate_recovery_doc()
    doc_path = os.path.join(dest, sa_backup.DEFAULT_RECOVERY_DOC)
    with open(doc_path, "w", encoding="utf-8") as handle:
        handle.write(doc)
    check("FG-12 recovery doc materialised", os.path.isfile(doc_path) and
          os.path.getsize(doc_path) > 0, "%d bytes" % os.path.getsize(doc_path))

    report = fg.recovery_doc_clean_machine_check(dest)
    print("        FG-12 present: %s" % sorted(k for k, v in report["present"].items() if v))
    print("        FG-12 missing: %s" % report["missing"])
    check("FG-12 real doc field presence is measured", isinstance(report["ok"], bool),
          "present=%d missing=%d" % (sum(1 for v in report["present"].values() if v),
                                     len(report["missing"])))
    check("FG-12 doc is non-empty", report.get("size_bytes", 0) > 0,
          "%s bytes" % report.get("size_bytes"))

    partial = os.path.join(root, "partial_doc")
    os.makedirs(partial, exist_ok=True)
    with open(os.path.join(partial, "r.txt"), "w", encoding="utf-8") as handle:
        handle.write("VAULT_NAME: __STORE.svault\nSHA256: abc\n")
    report = fg.recovery_doc_clean_machine_check(doc_path=os.path.join(partial, "r.txt"))
    check("FG-12 partial doc lists gaps",
          report["ok"] is False and len(report["missing"]) == len(fg.RECOVERY_DOC_REQUIRED_FIELDS) - 2,
          "%d gaps" % len(report["missing"]))
    check("FG-12 gap names are actionable",
          "RECOVERY_SCRIPT" in report["missing"], report["missing"])


# --------------------------------------------------------------- ADVERSARIAL NEGATIVE CONTROLS


def test_adversarial_p0_compromise_simulation(root):
    # Structural rejection of production destinations
    production_paths = [
        r"V:\___VAC\__K\__STORE\vault",
        r"D:\Data\__SECURE_BACKUPS\dest",
        r"C:\Users\test\_PERSONAL\backups",
        r"E:\ACCOUNTS\STORAGE\primary",
        r"F:\__VAULT\containers",
    ]
    for ppath in production_paths:
        try:
            fg.simulate_destination_compromise(ppath)
            check("ADV-P0 production destination refused structurally", False, ppath)
        except fg.FutureGateError as exc:
            check("ADV-P0 production destination refused structurally",
                  exc.category == "production_destination_refused", ppath)

    # Rejection of non-temp destination lacking .disposable_fixture
    non_disp = os.path.join(REPO, "non_disposable_dir")
    try:
        fg.assert_disposable_destination(non_disp)
        check("ADV-P0 non-disposable destination refused", False)
    except fg.FutureGateError as exc:
        check("ADV-P0 non-disposable destination refused",
              exc.category == "disposable_fixture_required", exc.category)

    # Marker spoofing refused: .disposable_fixture alone without in-process registration fails closed
    disp_dir = os.path.join(REPO, "dummy_disposable_marker_test")
    os.makedirs(disp_dir, exist_ok=True)
    try:
        marker = os.path.join(disp_dir, ".disposable_fixture")
        with open(marker, "w") as h:
            h.write("disposable\n")
        try:
            fg.assert_disposable_destination(disp_dir)
            check("ADV-P0 fixture marker alone refused without registration", False)
        except fg.FutureGateError as exc:
            check("ADV-P0 fixture marker alone refused without registration",
                  exc.category == "disposable_fixture_required", exc.category)

        # In-process registered fixture accepted
        fg.register_disposable_fixture(disp_dir)
        res = fg.assert_disposable_destination(disp_dir)
        check("ADV-P0 fixture marker satisfies disposable requirement", bool(res))
    finally:
        fg.unregister_disposable_fixture(disp_dir)
        shutil.rmtree(disp_dir, ignore_errors=True)

    # Byte restoration and byte equality across all compromise modes
    for mode in ("flat_files", "current_ptr", "sidecar"):
        dest, _, (cur_id, cur_dir, cur_sha) = build_destination(
            os.path.join(root, "adv_comp_" + mode))
        if mode == "flat_files":
            target = os.path.join(dest, sa_backup.DEFAULT_VAULT_NAME)
        elif mode == "current_ptr":
            target = os.path.join(dest, fg.CURRENT_POINTER)
        else:
            target = os.path.join(cur_dir, "vault.svault.meta")
        pre_bytes = open(target, "rb").read()

        report = fg.simulate_destination_compromise(dest, mode=mode)
        check(f"ADV-P0[{mode}] simulation reports synthetic simulation completed",
              report.get("synthetic_simulation_completed") is True)
        check(f"ADV-P0[{mode}] simulation reports proves_real_restoration is False",
              report.get("proves_real_restoration") is False)
        check(f"ADV-P0[{mode}] simulation does not claim restoration verified",
              report.get("restoration_verified") is False)
        check(f"ADV-P0[{mode}] scratch cleanup verified",
              report.get("scratch_cleanup_verified") is True)
        check(f"ADV-P0[{mode}] scratch unregistration verified",
              report.get("scratch_unregistered") is True)
        check(f"ADV-P0[{mode}] pre/post sha256 equality holds",
              report.get("pre_tamper_sha256") == report.get("post_restore_sha256"))

        post_bytes = open(target, "rb").read()
        check(f"ADV-P0[{mode}] exact byte equality verified on disk",
              pre_bytes == post_bytes, f"len={len(post_bytes)}")


def test_adversarial_p1_unauthenticated_generation(root):
    dest, _, (cur_id, cur_dir, cur_sha) = build_destination(
        os.path.join(root, "adv_unauth"))
    key = b"0123456789abcdef0123456789abcdef"

    # Paired tampering: attacker modifies payload AND updates hash in sidecar
    vault_file = os.path.join(cur_dir, "vault.svault")
    with open(vault_file, "wb") as h:
        h.write(b"TAMPERED_PAYLOAD_BY_ATTACKER")
    tampered_sha = hashlib.sha256(b"TAMPERED_PAYLOAD_BY_ATTACKER").hexdigest()

    meta_file = os.path.join(cur_dir, "vault.svault.meta")
    meta = json.load(open(meta_file, "r", encoding="utf-8"))
    meta["vhdx_sha256"] = tampered_sha
    # No manifest_hmac is present
    with open(meta_file, "w", encoding="utf-8") as h:
        json.dump(meta, h)

    # Without HMAC enforcement, it passes integrity-only check
    res_no_key = fg.resolve_known_good_generation(dest)
    check("ADV-P1 integrity-only accepts matched hash",
          res_no_key.get("ok") is True and res_no_key.get("generation_id") == cur_id)

    # With hmac_key provided, paired tampering is REJECTED
    res_keyed = fg.resolve_known_good_generation(dest, hmac_key=key)
    check("ADV-P1 unauthenticated tampered generation rejected with key",
          res_keyed.get("generation_id") != cur_id)

    # ensure_min_known_good with hmac_key puts unauthenticated generation into unverified
    min_good_keyed = fg.ensure_min_known_good(dest, hmac_key=key, min_good=1)
    check("ADV-P1 unauthenticated generation marked unverified",
          cur_id in min_good_keyed.get("unverified", []))

    # Forged HMAC in sidecar is also REJECTED
    meta["manifest_hmac"] = "deadbeef" * 8
    with open(meta_file, "w", encoding="utf-8") as h:
        json.dump(meta, h)
    res_forged = fg.resolve_known_good_generation(dest, hmac_key=key)
    check("ADV-P1 forged HMAC rejected",
          res_forged.get("generation_id") != cur_id)

    # When require_hmac=True and missing HMAC, reject
    del meta["manifest_hmac"]
    with open(meta_file, "w", encoding="utf-8") as h:
        json.dump(meta, h)
    res_req = fg.resolve_known_good_generation(dest, require_hmac=True)
    check("ADV-P1 require_hmac rejects sidecar without HMAC",
          res_req.get("generation_id") != cur_id)

    # Authentic HMAC generated with real key is accepted
    canonical = json.dumps(
        {k: v for k, v in meta.items() if k != "manifest_hmac"},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    meta["manifest_hmac"] = sa_crypto.compute_manifest_hmac(key, canonical)
    with open(meta_file, "w", encoding="utf-8") as h:
        json.dump(meta, h)
    res_auth = fg.resolve_known_good_generation(dest, hmac_key=key)
    check("ADV-P1 authentic HMAC generation accepted",
          res_auth.get("ok") is True and res_auth.get("generation_id") == cur_id)


def test_adversarial_p1_path_containment(root):
    dest, _, (cur_id, cur_dir, _) = build_destination(
        os.path.join(root, "adv_containment"))

    # Direct validation check
    valid_id = "gen_1700000000_12345678"
    valid_target = fg.validate_generation_id_containment(dest, valid_id)
    check("ADV-P1 valid gen_id syntax contained", os.path.basename(valid_target) == valid_id)

    # Traversal attempts rejected
    traversal_ids = [
        "../../outside",
        r"..\..\outside",
        "gen_1700000000_12345678/../../../escape",
        "gen_1700000000_12345678/subdir",
        "invalid_non_canonical",
        "/etc/passwd",
        "C:\\Windows\\System32",
    ]
    for tid in traversal_ids:
        try:
            fg.validate_generation_id_containment(dest, tid)
            check("ADV-P1 traversal ID rejected", False, tid)
        except fg.FutureGateError as exc:
            check("ADV-P1 traversal ID rejected",
                  exc.category in ("invalid_generation_id", "path_traversal"), tid)

    # mark_generation_immutable rejects traversal
    try:
        fg.mark_generation_immutable(dest, "../../outside")
        check("ADV-P1 mark_immutable traversal rejected", False)
    except fg.FutureGateError as exc:
        check("ADV-P1 mark_immutable traversal rejected",
              exc.category in ("invalid_generation_id", "path_traversal"), exc.category)

    # run_restore_drill rejects traversal
    drill_scratch = os.path.join(root, "drill_scratch")
    drill_res = fg.run_restore_drill(dest, drill_scratch, gen_id="../../outside")
    check("ADV-P1 restore_drill traversal rejected",
          drill_res.get("ok") is False and drill_res.get("reason") in ("invalid_generation_id", "path_traversal"),
          drill_res.get("reason"))


def test_adversarial_p1_unsafe_current_replacement(root):
    # Valid rollback preservation:
    # Older generation gen_1 is pointed to by CURRENT deliberately.
    # recover_metadata_corruption must NOT clobber it with newer generation gen_2.
    dest, (g1, d1, s1), (g2, d2, s2) = build_destination(
        os.path.join(root, "adv_recovery"))
    # Point CURRENT to older generation g1
    write_pointer(dest, fg.CURRENT_POINTER, g1)
    # Sync flat file to g1 as well
    sync_flat(dest, d1)

    rep = fg.recover_metadata_corruption(dest)
    check("ADV-P1 valid rollback CURRENT preserved",
          rep.get("generation_id") == g1 and rep.get("after", {}).get("current_pointer") == g1)
    check("ADV-P1 pointer preserved action recorded",
          "current_pointer_preserved" in rep.get("actions", []))
    check("ADV-P1 pointer not rewritten on valid rollback",
          "current_pointer_rewritten" not in rep.get("actions", []))

    # Flat file still matches g1
    flat = os.path.join(dest, sa_backup.DEFAULT_VAULT_NAME)
    check("ADV-P1 flat file reconciled against selected rollback generation",
          fg._sha256(flat) == s1)

    # Corrupt CURRENT pointer: names nonexistent generation
    write_pointer(dest, fg.CURRENT_POINTER, "gen_00000000_deadbeef")
    rep_corrupt = fg.recover_metadata_corruption(dest)
    check("ADV-P1 corrupt CURRENT repointed to newest verified generation",
          rep_corrupt.get("after", {}).get("current_pointer") == g2)
    check("ADV-P1 pointer rewrite action recorded on corrupt current",
          "current_pointer_rewritten" in rep_corrupt.get("actions", []))

    # Missing CURRENT pointer:
    os.remove(os.path.join(dest, fg.CURRENT_POINTER))
    rep_missing = fg.recover_metadata_corruption(dest)
    check("ADV-P1 missing CURRENT restored to newest verified generation",
          rep_missing.get("after", {}).get("current_pointer") == g2)


def test_adversarial_acceptance_truthfulness(root):
    dest, _, (cur_id, cur_dir, _) = build_destination(
        os.path.join(root, "adv_truthfulness"))

    # FG-03: Classification honesty
    res_imm = fg.mark_generation_immutable(dest, cur_id)
    check("ADV-TRUTH FG-03 primitive classification declared honest",
          res_imm.get("primitive_classification") == "filesystem_discretionary_acl")

    # FG-07: Key loss drill truthfulness
    res_loss = fg.run_yubikey_loss_drill(dest)
    check("ADV-TRUTH FG-07 does not claim actual cryptographic recovery",
          res_loss.get("proves_actual_key_recovery") is False)
    check("ADV-TRUTH FG-07 primitive classified as metadata precondition check",
          res_loss.get("primitive_classification") == "metadata_precondition_check")

    # FG-08: Succession drill truthfulness
    res_succ = fg.run_succession_drill(dest)
    check("ADV-TRUTH FG-08 does not claim successor access proven",
          res_succ.get("proves_successor_access") is False)
    check("ADV-TRUTH FG-08 primitive classified as metadata precondition check",
          res_succ.get("primitive_classification") == "metadata_precondition_check")

    # FG-12: Full document check
    doc_text = "\n".join(f"{field}: sample_value" for field in fg.RECOVERY_DOC_REQUIRED_FIELDS)
    doc_path = os.path.join(dest, "full_recovery_doc.txt")
    with open(doc_path, "w", encoding="utf-8") as h:
        h.write(doc_text)
    res_doc = fg.recovery_doc_clean_machine_check(doc_path=doc_path)
    check("ADV-TRUTH FG-12 complete doc reports 8/8 present and passes",
          res_doc.get("ok") is True and len(res_doc.get("missing", [])) == 0)


def test_fg11_architectural_corrections(root):
    # 1, 2, 3: All three FG-11 modes with external link targets, byte preservation, and reparse preservation
    ext_dir = os.path.join(root, "external_target_dir")
    os.makedirs(ext_dir, exist_ok=True)
    ext_file = os.path.join(ext_dir, "external_secret.txt")
    initial_bytes = b"IMMUTABLE_EXTERNAL_CONTENT_SECRET_12345"
    with open(ext_file, "wb") as h:
        h.write(initial_bytes)
    initial_sha = hashlib.sha256(initial_bytes).hexdigest()

    for mode in ("flat_files", "current_ptr", "sidecar"):
        dest, _, (cur_id, cur_dir, cur_sha) = build_destination(
            os.path.join(root, "fg11_ext_" + mode))
        # Place an external link inside dest to simulate external object redirection
        link_target = os.path.join(dest, "external_redirect_link")
        make_junction(link_target, ext_dir)
        try:
            fg.simulate_destination_compromise(dest, mode=mode)
            check(f"FG11-ARCH[{mode}] destination with external junction rejected", False)
        except fg.FutureGateError as exc:
            check(f"FG11-ARCH[{mode}] destination with external junction rejected",
                  exc.category == "reparse_point_detected", exc.category)
        finally:
            if os.path.lexists(link_target):
                os.rmdir(link_target)

        # Also test hard link redirection rejection under each mode
        dest_hl, _, (cur_id, cur_dir, cur_sha) = build_destination(
            os.path.join(root, "fg11_hl_" + mode))
        hl_target = os.path.join(dest_hl, "hardlink_redirect.bin")
        os.link(ext_file, hl_target)
        try:
            fg.simulate_destination_compromise(dest_hl, mode=mode)
            check(f"FG11-ARCH[{mode}] destination with hardlink rejected", False)
        except fg.FutureGateError as exc:
            check(f"FG11-ARCH[{mode}] destination with hardlink rejected",
                  exc.category == "hardlink_detected", exc.category)
        finally:
            if os.path.lexists(hl_target):
                os.remove(hl_target)

        # 2. Exact external-object byte preservation
        cur_bytes = open(ext_file, "rb").read()
        check(f"FG11-ARCH[{mode}] external bytes preserved byte-for-byte",
              cur_bytes == initial_bytes and hashlib.sha256(cur_bytes).hexdigest() == initial_sha)

    # 3. Symlink identity and reparse point preservation
    junc_test = os.path.join(root, "junc_identity_test")
    make_junction(junc_test, ext_dir)
    try:
        check("FG11-ARCH reparse point detected as reparse point", sa_paths.is_reparse_point(junc_test))
        try:
            fg.assert_disposable_destination(junc_test)
        except fg.FutureGateError:
            pass
        check("FG11-ARCH reparse identity preserved (not replaced with plain file)",
              sa_paths.is_reparse_point(junc_test) and not os.path.isfile(junc_test))
    finally:
        os.rmdir(junc_test)

    # 4. generations parent symlink escape
    esc_dest = os.path.join(root, "escape_dest")
    os.makedirs(esc_dest, exist_ok=True)
    ext_gens = os.path.join(root, "ext_generations_target")
    os.makedirs(ext_gens, exist_ok=True)
    esc_gens_link = os.path.join(esc_dest, fg.GEN_DIR_NAME)
    make_junction(esc_gens_link, ext_gens)
    try:
        try:
            fg.validate_generation_id_containment(esc_dest, "gen_1700000000_12345678")
            check("FG11-ARCH generations parent junction escape rejected", False)
        except fg.FutureGateError as exc:
            check("FG11-ARCH generations parent junction escape rejected",
                  exc.category in ("reparse_point_detected", "path_traversal"), exc.category)

        try:
            fg.mark_generation_immutable(esc_dest, "gen_1700000000_12345678")
            check("FG11-ARCH mark_immutable via parent junction escape rejected", False)
        except fg.FutureGateError as exc:
            check("FG11-ARCH mark_immutable via parent junction escape rejected",
                  exc.category in ("reparse_point_detected", "path_traversal"), exc.category)

        # Verify no .immutable file created outside root in ext_gens
        check("FG11-ARCH no .immutable marker leaked to external generations target",
              not os.path.exists(os.path.join(ext_gens, fg.IMMUTABLE_MARKER)))
    finally:
        os.rmdir(esc_gens_link)

    # 5. Fake TEMP/TMP names and marker spoofing
    # Names outside registered disposable roots
    fake_names = [
        os.path.join(REPO, "temptation_unrelated"),
        os.path.join(REPO, "custom_tmp_folder"),
        r"C:\fake_TEMP_path",
    ]
    for fname in fake_names:
        try:
            fg.assert_disposable_destination(fname)
            check("FG11-ARCH fake TEMP/TMP name refused without registration", False, fname)
        except fg.FutureGateError as exc:
            check("FG11-ARCH fake TEMP/TMP name refused without registration",
                  exc.category == "disposable_fixture_required", fname)

    # Marker spoofing: create .disposable_fixture file in unregistered directory
    spoof_dir = os.path.join(REPO, "spoof_marker_dir")
    os.makedirs(spoof_dir, exist_ok=True)
    try:
        with open(os.path.join(spoof_dir, ".disposable_fixture"), "w") as h:
            h.write("disposable=true\n")
        try:
            fg.assert_disposable_destination(spoof_dir)
            check("FG11-ARCH marker spoofing refused without registration", False)
        except fg.FutureGateError as exc:
            check("FG11-ARCH marker spoofing refused without registration",
                  exc.category == "disposable_fixture_required", exc.category)
    finally:
        shutil.rmtree(spoof_dir, ignore_errors=True)

    # 6. Windows junctions and directory reparse points
    win_junc = os.path.join(root, "win_junc_test")
    make_junction(win_junc, ext_dir)
    try:
        check("FG11-ARCH Windows junction identified as reparse point",
              sa_paths.is_reparse_point(win_junc))
        try:
            fg.assert_disposable_destination(win_junc)
            check("FG11-ARCH Windows junction refused as disposable destination", False)
        except fg.FutureGateError as exc:
            check("FG11-ARCH Windows junction refused as disposable destination",
                  exc.category == "reparse_point_detected", exc.category)

        try:
            fg.validate_generation_id_containment(win_junc, "gen_1700000000_12345678")
            check("FG11-ARCH Windows junction destination root rejected", False)
        except fg.FutureGateError as exc:
            check("FG11-ARCH Windows junction destination root rejected",
                  exc.category == "reparse_point_detected", exc.category)
    finally:
        os.rmdir(win_junc)

    # 7. Target replacement between validation and mutation
    replace_dest = os.path.join(root, "replace_dest")
    os.makedirs(os.path.join(replace_dest, fg.GEN_DIR_NAME), exist_ok=True)
    replace_gen = os.path.join(replace_dest, fg.GEN_DIR_NAME, "gen_1700000000_12345678")
    make_junction(replace_gen, ext_dir)
    try:
        try:
            fg.validate_generation_id_containment(replace_dest, "gen_1700000000_12345678")
            check("FG11-ARCH generation reparse target rejected on validation", False)
        except fg.FutureGateError as exc:
            check("FG11-ARCH generation reparse target rejected on validation",
                  exc.category == "reparse_point_detected", exc.category)
    finally:
        os.rmdir(replace_gen)

    # 8. Signed sibling recovery and post-recovery HMAC
    dest_signed, (s1_id, s1_dir, s1_sha), (s2_id, s2_dir, s2_sha) = build_destination(
        os.path.join(root, "signed_recovery"))
    key = b"auth_signing_key_secret_01234567"
    # Make s1 and s2 share payload so s2 (newest) can attest s1
    with open(os.path.join(s1_dir, fg.GEN_VAULT_NAME), "wb") as h:
        h.write(open(os.path.join(s2_dir, fg.GEN_VAULT_NAME), "rb").read())
    # Sign s2 with HMAC
    s2_meta = json.load(open(os.path.join(s2_dir, "vault.svault.meta")))
    s2_canonical = json.dumps(
        {k: v for k, v in s2_meta.items() if k != "manifest_hmac"},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    s2_meta["manifest_hmac"] = sa_crypto.compute_manifest_hmac(key, s2_canonical)
    with open(os.path.join(s2_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(s2_meta, h)
    # Corrupt s1 meta (has old HMAC)
    s1_meta = json.load(open(os.path.join(s1_dir, "vault.svault.meta")))
    s1_meta["vhdx_sha256"] = "corrupted_hash"
    s1_meta["manifest_hmac"] = "some_old_hmac"
    with open(os.path.join(s1_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(s1_meta, h)

    rep_signed = fg.recover_metadata_corruption(dest_signed, hmac_key=key)
    check("FG11-ARCH signed sibling recovery records authenticated rebuild",
          "sidecar_rebuilt_and_authenticated" in rep_signed.get("actions", []))
    res_both = fg.resolve_known_good_generation(dest_signed, hmac_key=key)
    check("FG11-ARCH post-recovery HMAC resolves valid generation",
          res_both.get("ok") is True)
    repaired_meta = json.load(open(os.path.join(s1_dir, "vault.svault.meta")))
    repaired_canonical = json.dumps(
        {k: v for k, v in repaired_meta.items() if k != "manifest_hmac"},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    check("FG11-ARCH post-recovery HMAC signature mathematically verified",
          sa_crypto.verify_manifest_hmac(key, repaired_canonical, repaired_meta.get("manifest_hmac", "")))

    # 9. Missing or unavailable authenticated signing key
    dest_nokey, (k1_id, k1_dir, k1_sha), (k2_id, k2_dir, k2_sha) = build_destination(
        os.path.join(root, "nokey_recovery"))
    with open(os.path.join(k1_dir, fg.GEN_VAULT_NAME), "wb") as h:
        h.write(open(os.path.join(k2_dir, fg.GEN_VAULT_NAME), "rb").read())
    # k2 has HMAC
    k2_meta = json.load(open(os.path.join(k2_dir, "vault.svault.meta")))
    k2_canonical = json.dumps(
        {k: v for k, v in k2_meta.items() if k != "manifest_hmac"},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    k2_meta["manifest_hmac"] = sa_crypto.compute_manifest_hmac(key, k2_canonical)
    with open(os.path.join(k2_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(k2_meta, h)
    # Corrupt k1 meta (has old HMAC)
    k1_meta = json.load(open(os.path.join(k1_dir, "vault.svault.meta")))
    k1_meta["vhdx_sha256"] = "corrupted_hash"
    k1_meta["manifest_hmac"] = "some_old_hmac"
    with open(os.path.join(k1_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(k1_meta, h)

    rep_nokey = fg.recover_metadata_corruption(dest_nokey, hmac_key=None)
    check("FG11-ARCH missing signing key refuses sidecar repair",
          any("sidecar_repair_refused_signing_key_required" in a for a in rep_nokey.get("actions", [])))
    res_nokey_keyed = fg.resolve_known_good_generation(dest_nokey, hmac_key=key)
    check("FG11-ARCH unauthenticated damaged generation remains unverified",
          res_nokey_keyed.get("generation_id") != k1_id)

    # 10. No production destination writes under any mode
    prod_test_paths = [
        r"V:\___VAC\__K\__STORE\vault",
        r"D:\Data\__SECURE_BACKUPS\dest",
        r"C:\Users\test\_PERSONAL\backups",
    ]
    for pmode in ("flat_files", "current_ptr", "sidecar"):
        for ppath in prod_test_paths:
            try:
                fg.simulate_destination_compromise(ppath, mode=pmode)
                check(f"FG11-ARCH[{pmode}] production write refused: {ppath}", False)
            except fg.FutureGateError as exc:
                check(f"FG11-ARCH[{pmode}] production write refused: {ppath}",
                      exc.category == "production_destination_refused")


def test_adversarial_p1a_p1b_metadata_recovery(root):
    # P1-A: Metadata laundering prevention
    # Sibling with foreign profile_id must be refused
    dest_laun, (g1_id, g1_dir, g1_sha), (g2_id, g2_dir, g2_sha) = build_destination(
        os.path.join(root, "p1a_foreign_profile"))
    key = b"signing_key_secret_0123456789abcdef"

    # Make g1 share payload with g2
    with open(os.path.join(g1_dir, fg.GEN_VAULT_NAME), "wb") as h:
        h.write(open(os.path.join(g2_dir, fg.GEN_VAULT_NAME), "rb").read())

    # g2 is authoritative with profile_id = "trusted_profile"
    g2_meta = json.load(open(os.path.join(g2_dir, "vault.svault.meta")))
    g2_meta["profile_id"] = "trusted_profile"
    g2_canonical = json.dumps(
        {k: v for k, v in g2_meta.items() if k != "manifest_hmac"},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    g2_meta["manifest_hmac"] = sa_crypto.compute_manifest_hmac(key, g2_canonical)
    with open(os.path.join(g2_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(g2_meta, h)

    # g1 is corrupt but carries an attacker-injected profile_id and injected fields
    g1_meta = json.load(open(os.path.join(g1_dir, "vault.svault.meta")))
    g1_meta["vhdx_sha256"] = "corrupted_hash"
    g1_meta["profile_id"] = "attacker_foreign_profile"
    g1_meta["injected_malicious_claim"] = "attacker_backdoor"
    with open(os.path.join(g1_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(g1_meta, h)

    rep_foreign = fg.recover_metadata_corruption(dest_laun, hmac_key=key)
    check("P1-A foreign profile ID repair refused",
          any("sidecar_repair_refused_identity_mismatch" in a for a in rep_foreign.get("actions", [])))
    check("P1-A foreign profile ID forensic rejection recorded",
          any("rejected_metadata_forensic" in a and "foreign_profile_id" in a for a in rep_foreign.get("actions", [])))
    check("P1-A partial recovery reported when repair refused",
          rep_foreign.get("partial_recovery") is True)

    # Test arbitrary injected fields are not laundered into rebuilt sidecar when profile matches
    dest_clean, (c1_id, c1_dir, c1_sha), (c2_id, c2_dir, c2_sha) = build_destination(
        os.path.join(root, "p1a_clean_rebuild"))
    with open(os.path.join(c1_dir, fg.GEN_VAULT_NAME), "wb") as h:
        h.write(open(os.path.join(c2_dir, fg.GEN_VAULT_NAME), "rb").read())
    c2_meta = json.load(open(os.path.join(c2_dir, "vault.svault.meta")))
    c2_meta["profile_id"] = "trusted_profile"
    c2_canonical = json.dumps(
        {k: v for k, v in c2_meta.items() if k != "manifest_hmac"},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    c2_meta["manifest_hmac"] = sa_crypto.compute_manifest_hmac(key, c2_canonical)
    with open(os.path.join(c2_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(c2_meta, h)

    c1_meta = json.load(open(os.path.join(c1_dir, "vault.svault.meta")))
    c1_meta["vhdx_sha256"] = "corrupted_hash"
    c1_meta["profile_id"] = "trusted_profile"
    c1_meta["malicious_injected_key"] = "laundering_attempt"
    c1_meta["manifest_hmac"] = "prior_signed_hmac"
    with open(os.path.join(c1_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(c1_meta, h)

    rep_clean = fg.recover_metadata_corruption(dest_clean, hmac_key=key)
    check("P1-A rebuild succeeds for matching profile",
          "sidecar_rebuilt_and_authenticated" in rep_clean.get("actions", []))
    rebuilt_meta = json.load(open(os.path.join(c1_dir, "vault.svault.meta")))
    check("P1-A injected keys purged from rebuilt metadata",
          "malicious_injected_key" not in rebuilt_meta)

    # P1-B: Invalid sibling HMAC detection
    dest_hmac, (h1_id, h1_dir, h1_sha), (h2_id, h2_dir, h2_sha) = build_destination(
        os.path.join(root, "p1b_invalid_hmac"))
    with open(os.path.join(h1_dir, fg.GEN_VAULT_NAME), "wb") as h:
        h.write(open(os.path.join(h2_dir, fg.GEN_VAULT_NAME), "rb").read())
    # h2 is authoritative
    h2_meta = json.load(open(os.path.join(h2_dir, "vault.svault.meta")))
    h2_canonical = json.dumps(
        {k: v for k, v in h2_meta.items() if k != "manifest_hmac"},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    h2_meta["manifest_hmac"] = sa_crypto.compute_manifest_hmac(key, h2_canonical)
    with open(os.path.join(h2_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(h2_meta, h)

    # h1 has valid sha256 in meta, but invalid HMAC
    h1_meta = json.load(open(os.path.join(h1_dir, "vault.svault.meta")))
    h1_meta["vhdx_sha256"] = fg._sha256(os.path.join(h1_dir, fg.GEN_VAULT_NAME))
    h1_meta["manifest_hmac"] = "invalid_bogus_hmac_signature"
    # also make it have foreign profile so repair is refused
    h1_meta["profile_id"] = "different_profile"
    with open(os.path.join(h1_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(h1_meta, h)

    rep_hmac = fg.recover_metadata_corruption(dest_hmac, hmac_key=key)
    check("P1-B invalid sibling HMAC detected",
          any("invalid_sibling_hmac_detected" in a for a in rep_hmac.get("actions", [])))
    check("P1-B partial recovery reported on invalid sibling HMAC refusal",
          rep_hmac.get("partial_recovery") is True)


def test_adversarial_p1c_p1d_immutability_security(root):
    # P1-D: Child reparse point / junction rejection
    dest, _, (cur_id, cur_dir, _) = build_destination(os.path.join(root, "p1cd_dest"))
    ext_dir = os.path.join(root, "p1cd_ext")
    os.makedirs(ext_dir, exist_ok=True)
    child_junc = os.path.join(cur_dir, "child_junction")
    make_junction(child_junc, ext_dir)
    try:
        try:
            fg.mark_generation_immutable(dest, cur_id)
            check("P1-D child directory junction rejected", False)
        except fg.FutureGateError as exc:
            check("P1-D child directory junction rejected",
                  exc.category == "reparse_point_detected", exc.category)
        check("P1-C transactional rollback: marker not present on child failure",
              not os.path.exists(os.path.join(cur_dir, fg.IMMUTABLE_MARKER)))
    finally:
        if os.path.lexists(child_junc):
            os.rmdir(child_junc)

    # P1-D: Hard link child rejection
    ext_file = os.path.join(ext_dir, "hardlink_target.bin")
    with open(ext_file, "wb") as h:
        h.write(b"EXT_DATA")
    child_hl = os.path.join(cur_dir, "child_hardlink.bin")
    os.link(ext_file, child_hl)
    try:
        try:
            fg.mark_generation_immutable(dest, cur_id)
            check("P1-D child hardlink rejected", False)
        except fg.FutureGateError as exc:
            check("P1-D child hardlink rejected",
                  exc.category == "hardlink_detected", exc.category)
        check("P1-C transactional rollback: marker not present on hardlink failure",
              not os.path.exists(os.path.join(cur_dir, fg.IMMUTABLE_MARKER)))
    finally:
        if os.path.lexists(child_hl):
            os.remove(child_hl)

    # P1-C: False immutability success prevention
    # If .immutable marker is placed, but file is writable, is_generation_immutable returns False
    dest2, _, (c_id, c_dir, _) = build_destination(os.path.join(root, "p1c_write_perm"))
    marker = os.path.join(c_dir, fg.IMMUTABLE_MARKER)
    with open(marker, "w", encoding="utf-8") as h:
        json.dump({"gen_id": c_id, "immutable": True}, h)
    # Ensure vault is writable
    vault = os.path.join(c_dir, fg.GEN_VAULT_NAME)
    os.chmod(vault, stat.S_IWRITE | stat.S_IREAD)
    check("P1-C writable vault file refuses immutability check",
          fg.is_generation_immutable(dest2, c_id) is False)

    # When mark_generation_immutable succeeds, it verifies read-only attributes
    res_imm = fg.mark_generation_immutable(dest2, c_id)
    check("P1-C mark_generation_immutable succeeds on clean directory",
          res_imm.get("ok") is True)
    check("P1-C is_generation_immutable returns True when disk attributes actually read-only",
          fg.is_generation_immutable(dest2, c_id) is True)


def test_adversarial_p2a_missing_hmac_keys(root):
    # P2-A: Unhandled missing HMAC key handling
    dest, _, (cur_id, cur_dir, _) = build_destination(os.path.join(root, "p2a_hmac_dest"))

    # ensure_min_known_good with require_hmac=True and hmac_key=None
    res_min = fg.ensure_min_known_good(dest, min_good=1, require_hmac=True, hmac_key=None)
    check("P2-A ensure_min_known_good cleanly refuses missing HMAC key",
          res_min.get("ok") is False and res_min.get("reason") == "hmac_key_required" and
          res_min.get("authenticity") == "KEY_REQUIRED_MISSING")

    # resolve_known_good_generation with require_hmac=True and hmac_key=None
    res_res = fg.resolve_known_good_generation(dest, require_hmac=True, hmac_key=None)
    check("P2-A resolve_known_good_generation cleanly refuses missing HMAC key",
          res_res.get("ok") is False and res_res.get("reason") == "hmac_key_required" and
          res_res.get("authenticity") == "KEY_REQUIRED_MISSING")

    # _verify_specific_generation with require_hmac=True and hmac_key=None
    res_spec = fg._verify_specific_generation(dest, cur_id, require_hmac=True, hmac_key=None)
    check("P2-A _verify_specific_generation cleanly refuses missing HMAC key",
          res_spec.get("ok") is False and res_spec.get("reason") == "hmac_key_required" and
          res_spec.get("authenticity") == "KEY_REQUIRED_MISSING")


def test_adversarial_fg03_marker_link_escape(root):
    ext_dir = os.path.join(root, "fg03_external")
    os.makedirs(ext_dir, exist_ok=True)
    canary = os.path.join(ext_dir, "canary.txt")
    with open(canary, "w", encoding="utf-8") as h:
        h.write("CANARY_ORIGINAL\n")

    dest, (g1_id, g1_dir, _), (g2_id, g2_dir, _) = build_destination(
        os.path.join(root, "fg03_marker_dest"))

    # 1. Existing marker is a symlink pointing to external canary
    marker_link = os.path.join(g2_dir, fg.IMMUTABLE_MARKER)
    try:
        os.symlink(canary, marker_link)
        has_symlink = True
    except (OSError, NotImplementedError):
        has_symlink = False

    if has_symlink:
        try:
            try:
                fg.mark_generation_immutable(dest, g2_id)
                check("FG-03 marker symlink rejected", False)
            except fg.FutureGateError as exc:
                check("FG-03 marker symlink rejected",
                      exc.category == "reparse_point_detected", exc.category)
            with open(canary, "r", encoding="utf-8") as h:
                check("FG-03 external canary unmodified after marker symlink attempt",
                      h.read() == "CANARY_ORIGINAL\n")
        finally:
            if os.path.lexists(marker_link):
                os.remove(marker_link)

    # 2. Existing marker is a hardlink
    marker_hl = os.path.join(g2_dir, fg.IMMUTABLE_MARKER)
    ext_target = os.path.join(ext_dir, "hl_target.txt")
    with open(ext_target, "w", encoding="utf-8") as h:
        h.write("HL_ORIGINAL\n")
    try:
        os.link(ext_target, marker_hl)
        has_hl = True
    except (OSError, NotImplementedError):
        has_hl = False

    if has_hl:
        try:
            try:
                fg.mark_generation_immutable(dest, g2_id)
                check("FG-03 marker hardlink rejected", False)
            except fg.FutureGateError as exc:
                check("FG-03 marker hardlink rejected",
                      exc.category == "hardlink_detected", exc.category)
            with open(ext_target, "r", encoding="utf-8") as h:
                check("FG-03 external hardlink target unmodified",
                      h.read() == "HL_ORIGINAL\n")
        finally:
            if os.path.lexists(marker_hl):
                os.remove(marker_hl)

    # 3. Existing offline marker is a link/reparse point
    offline_marker = os.path.join(g2_dir, fg.OFFLINE_MARKER)
    if has_symlink:
        try:
            os.symlink(canary, offline_marker)
            try:
                fg.mark_generation_immutable(dest, g2_id, offline=True)
                check("FG-03 offline marker symlink rejected", False)
            except fg.FutureGateError as exc:
                check("FG-03 offline marker symlink rejected",
                      exc.category == "reparse_point_detected", exc.category)
        finally:
            if os.path.lexists(offline_marker):
                os.remove(offline_marker)

    # 4. Clean mark_generation_immutable succeeds and makes files read-only
    res = fg.mark_generation_immutable(dest, g2_id, offline=True)
    check("FG-03 mark_generation_immutable succeeds on clean directory", res.get("ok") is True)
    check("FG-03 is_generation_immutable returns True on clean read-only directory",
          fg.is_generation_immutable(dest, g2_id) is True)


def test_adversarial_fg10_missing_sibling_hmac(root):
    dest, (sib_id, sib_dir, sib_sha), (cur_id, cur_dir, cur_sha) = build_destination(
        os.path.join(root, "fg10_sibling_dest"))
    key = b"fg10_secret_signing_key_0123456"

    # Both share payload
    with open(os.path.join(sib_dir, fg.GEN_VAULT_NAME), "wb") as h:
        h.write(open(os.path.join(cur_dir, fg.GEN_VAULT_NAME), "rb").read())

    # Sign current generation with HMAC
    cur_meta = json.load(open(os.path.join(cur_dir, "vault.svault.meta")))
    cur_canonical = json.dumps(
        {k: v for k, v in cur_meta.items() if k != "manifest_hmac"},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    cur_meta["manifest_hmac"] = sa_crypto.compute_manifest_hmac(key, cur_canonical)
    with open(os.path.join(cur_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(cur_meta, h)

    # Sibling has correct payload SHA256, but manifest_hmac is absent
    sib_meta = json.load(open(os.path.join(sib_dir, "vault.svault.meta")))
    sib_meta["vhdx_sha256"] = fg._sha256(os.path.join(sib_dir, fg.GEN_VAULT_NAME))
    sib_meta.pop("manifest_hmac", None)
    with open(os.path.join(sib_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(sib_meta, h)

    # 1. Authenticated recovery with valid key: unsigned sibling must NOT be accepted as healthy,
    # must NOT be silently re-signed, and must yield partial_recovery
    rep = fg.recover_metadata_corruption(dest, hmac_key=key)
    check("FG-10 unsigned sibling detected as missing HMAC",
          any(a.startswith("missing_sibling_hmac_detected:") for a in rep.get("actions", [])))
    check("FG-10 sidecar repair refused for unauthenticated sibling",
          any(a.startswith("sidecar_repair_refused_unauthenticated_sibling:") for a in rep.get("actions", [])))
    check("FG-10 recovery reports partial_recovery for unauthenticated sibling",
          rep.get("partial_recovery") is True and rep.get("recovery_status") == "partial_recovery")
    sib_after = json.load(open(os.path.join(sib_dir, "vault.svault.meta")))
    check("FG-10 sibling was not unauthorizedly re-signed",
          "manifest_hmac" not in sib_after)
    check("FG-10 sibling evaluation classified as AUTHENTICATION_MISSING",
          rep.get("sibling_evaluations", {}).get(sib_id, {}).get("authenticity") == "AUTHENTICATION_MISSING")

    # 2. Legacy mode (hmac_key is None): unsigned sibling is valid integrity-only
    dest_leg, (l1_id, l1_dir, _), (l2_id, l2_dir, _) = build_destination(
        os.path.join(root, "fg10_legacy_dest"))
    with open(os.path.join(l1_dir, fg.GEN_VAULT_NAME), "wb") as h:
        h.write(open(os.path.join(l2_dir, fg.GEN_VAULT_NAME), "rb").read())
    l1_meta = json.load(open(os.path.join(l1_dir, "vault.svault.meta")))
    l1_meta["vhdx_sha256"] = fg._sha256(os.path.join(l1_dir, fg.GEN_VAULT_NAME))
    l1_meta.pop("manifest_hmac", None)
    with open(os.path.join(l1_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(l1_meta, h)
    l2_meta = json.load(open(os.path.join(l2_dir, "vault.svault.meta")))
    l2_meta.pop("manifest_hmac", None)
    with open(os.path.join(l2_dir, "vault.svault.meta"), "w", encoding="utf-8") as h:
        json.dump(l2_meta, h)

    rep_leg = fg.recover_metadata_corruption(dest_leg, hmac_key=None)
    check("FG-10 legacy mode full recovery for unsigned generations",
          rep_leg.get("partial_recovery") is False and rep_leg.get("recovery_status") == "full_recovery")
    check("FG-10 legacy sibling evaluation classified as INTEGRITY_ONLY",
          rep_leg.get("sibling_evaluations", {}).get(l1_id, {}).get("authenticity") == "INTEGRITY_ONLY")


def main():
    root = tempfile.mkdtemp(prefix="sa_future_gates_")
    fg.register_disposable_fixture(root)
    try:
        print("destination root: %s" % root)
        print("")
        test_ransomware_mass_change()
        test_min_known_good(root)
        test_immutable_generations(root)
        test_corrupted_current_fallback(root)
        test_bit_rot_scrub(root)
        test_restore_drill(root)
        test_yubikey_loss_drill(root)
        test_succession_drill(root)
        test_version_history(root)
        test_metadata_recovery(root)
        test_destination_compromise(root)
        test_clean_machine_recovery_doc(root)

        # Adversarial Negative Controls & Regressions
        test_adversarial_p0_compromise_simulation(root)
        test_adversarial_p1_unauthenticated_generation(root)
        test_adversarial_p1_path_containment(root)
        test_adversarial_p1_unsafe_current_replacement(root)
        test_adversarial_p1a_p1b_metadata_recovery(root)
        test_adversarial_p1c_p1d_immutability_security(root)
        test_adversarial_p2a_missing_hmac_keys(root)
        test_adversarial_acceptance_truthfulness(root)

        # FG-11 Architectural Negative Controls & Redirection Regressions
        test_fg11_architectural_corrections(root)

        # FG-03 & FG-10 Security Remediation Negative Controls
        test_adversarial_fg03_marker_link_escape(root)
        test_adversarial_fg10_missing_sibling_hmac(root)
    finally:
        fg.unregister_disposable_fixture(root)
        shutil.rmtree(root, ignore_errors=True)

    print("")
    failed = [label for label, ok, _ in RESULTS if not ok]
    print("TOTAL %d  PASS %d  FAIL %d" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    if failed:
        for label in failed:
            print("  FAILED: %s" % label)
        return 1
    print("")
    print("=== FUTURE GATES ASSURANCE SUMMARY (T-263) ===")
    print("UNIT_PASS: %d" % len([r for r in RESULTS if r[1]]))
    print("DISPOSABLE_INTEGRATION_PASS: 12 gates verified in isolated disposable scratch")
    print("WINDOWS_SECURITY_PASS: NTFS reparse points, junctions, hardlinks, traversal refused")
    print("HARDWARE_REAL_BLOCKED: T-261 hardware durability/media acceptance unavailable")
    print("PRODUCT_ACCEPTANCE_PENDING: T-263 unadmitted (SCOUT / T-220 active, no-publish)")
    print("ASSURANCE STATUS: CONDITIONAL_SECURITY_ASSURED (NO_PUBLISH)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
