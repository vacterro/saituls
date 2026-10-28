"""T-256: capacity truth and the guarded grow plan.

The rule the ticket turns on: a measured value and a configured value are
different facts, "unknown" is a real answer and never a fabricated zero, and a
grow runs only on a fully passed plan.

These are Windows tests where Windows behaviour is the subject. The two
confirmed defects lived in ``host_volume_of`` (a doubled colon, and a colon
bolted onto a UNC share) and in treating a file's length as its capacity, so
neither of those paths is mocked away: the drive-normalization cases call the
real function, and the measurement cases create a real dynamic VHDX through
virtdisk and read it back. Only genuine *environment* probes -- the Hyper-V
toolchain and the virtdisk size reply for the fixed-allocation decision -- are
substituted, and each substitution is labelled where it happens.
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Scripts" / "secure_apps"))

import sa_cli        # noqa: E402
import sa_capacity   # noqa: E402

try:
    import sa_win   # noqa: E402
except Exception:    # pragma: no cover - host gap
    sa_win = None

WINDOWS = os.name == "nt"
VIRTDISK = bool(sa_win is not None and getattr(sa_win, "virtdisk", None))
BS = chr(92)
J = BS.join

_NEEDS_VHDX = unittest.skipUnless(
    WINDOWS and VIRTDISK,
    "needs Windows with virtdisk.dll to create a real VHDX fixture")

_TOOLCHAIN_READY = {"powershell": True, "resize_vhd": True, "get_vhd": True,
                    "available": True, "detail": ""}


def _unc(*parts):
    return BS * 2 + J(parts)


def _extended(*parts):
    return BS * 2 + J(("?",) + parts)


class HostVolumeNormalizationTests(unittest.TestCase):
    """The confirmed defect: ``drive + ":"`` produced ``C::`` and UNC junk."""

    def test_drive_root_and_nested_paths(self):
        self.assertEqual(sa_capacity.host_volume_of(J(["C:", "v.vhdx"])), "C:")
        self.assertEqual(
            sa_capacity.host_volume_of(J(["V:", "deep", "tree", "v.vhdx"])), "V:")
        self.assertEqual(sa_capacity.host_volume_of("C:" + BS), "C:")
        self.assertEqual(sa_capacity.host_volume_of("C:"), "C:")

    def test_drive_letter_is_never_doubled(self):
        # the exact regression: "C:" must not become "C::"
        for probe in (J(["C:", "v.vhdx"]), "C:" + BS, "C:"):
            volume = sa_capacity.host_volume_of(probe)
            self.assertNotIn("::", volume)
            self.assertTrue(sa_capacity._volume_root(volume).endswith(BS))

    def test_lowercase_drive_is_uppercased_once(self):
        self.assertEqual(sa_capacity.host_volume_of(J(["c:", "v.vhdx"])), "C:")

    def test_relative_path_resolves_to_its_real_volume(self):
        volume = sa_capacity.host_volume_of(J(["relative", "v.vhdx"]))
        self.assertIsNotNone(volume)
        self.assertRegex(volume, r"^[A-Z]:$")
        self.assertNotIn("::", volume)

    def test_unc_path_gets_no_fabricated_drive_letter(self):
        volume = sa_capacity.host_volume_of(_unc("server", "share", "v.vhdx"))
        self.assertEqual(volume, _unc("server", "share"))
        self.assertNotIn(":", volume)

    def test_extended_length_paths(self):
        self.assertEqual(sa_capacity.host_volume_of(_extended("C:", "v.vhdx")), "C:")
        self.assertEqual(
            sa_capacity.host_volume_of(_extended("UNC", "server", "share", "v.vhdx")),
            _unc("server", "share"))

    def test_device_namespace_has_no_volume(self):
        self.assertIsNone(
            sa_capacity.host_volume_of(_unc(".", "PhysicalDrive0")))

    def test_empty_and_missing_paths_are_none(self):
        self.assertIsNone(sa_capacity.host_volume_of(""))
        self.assertIsNone(sa_capacity.host_volume_of(None))

    def test_volume_root_of_a_missing_path_is_still_the_drive(self):
        volume = sa_capacity.host_volume_of(J(["C:", "nope", "missing.vhdx"]))
        self.assertEqual(volume, "C:")


class MissingAndCorruptContainerTests(unittest.TestCase):

    def test_missing_container_reports_missing_not_zero(self):
        facts = sa_capacity.container_facts(J(["C:", "nope", "missing.vhdx"]), 16)
        self.assertFalse(facts["exists"])
        self.assertIn("missing", sa_capacity.classify(facts))
        self.assertIsNone(facts["file_length_gb"])
        self.assertIsNone(facts["virtual_capacity_gb"])
        self.assertIsNone(facts["host_free_gb"])

    def test_missing_container_is_refused_before_the_plan(self):
        path = J(["C:", "nope", "missing.vhdx"])
        plan, refusal = sa_capacity.plan_grow(path, 16, 32)
        self.assertIsNone(plan)
        self.assertIn("not found", refusal)

    def test_invalid_target_is_refused_before_existence(self):
        # argument sanity is judged first, so a nonsense size is reported as a
        # nonsense size rather than as a missing file
        plan, refusal = sa_capacity.plan_grow(J(["C:", "nope", "x.vhdx"]), 16, 16.5)
        self.assertIsNone(plan)
        self.assertIn("whole number", refusal)

    def test_zero_target_is_refused(self):
        plan, refusal = sa_capacity.plan_grow(J(["C:", "nope", "x.vhdx"]), 16, 0)
        self.assertIsNone(plan)
        self.assertIn("greater than zero", refusal)

    def test_target_equal_to_current_on_a_missing_container_reports_missing(self):
        # a real larger-than-current comparison needs a real measurement and
        # lives in GrowPlanTests; here the file is simply absent
        plan, refusal = sa_capacity.plan_grow(J(["C:", "nope", "x.vhdx"]), 16, 16)
        self.assertIsNone(plan)
        self.assertIn("not found", refusal)

    def test_oversized_target_is_refused(self):
        plan, refusal = sa_capacity.plan_grow(
            J(["C:", "nope", "x.vhdx"]), 16, sa_capacity.MAX_GROW_GB + 1)
        self.assertIsNone(plan)
        self.assertIn("VHDX maximum", refusal)

    def test_corrupt_image_is_identified_and_refused(self):
        tmp = tempfile.mkdtemp(prefix="saituls-cap-bad-")
        path = os.path.join(tmp, "corrupt.vhdx")
        with open(path, "wb") as handle:
            handle.write(b"this is not a virtual disk" * 32)
        try:
            facts = sa_capacity.container_facts(path, 16)
            self.assertTrue(facts["exists"])
            self.assertEqual(facts["format"], "invalid")
            self.assertFalse(facts["format_valid"])
            self.assertIn("container_format_invalid", sa_capacity.classify(facts))
            plan, refusal = sa_capacity.plan_grow(path, 16, 32, facts=facts)
            self.assertIsNone(plan)
            self.assertIn("not a valid VHDX", refusal)
        finally:
            os.remove(path)
            os.rmdir(tmp)

    def test_unreadable_host_volume_leaves_free_space_unknown(self):
        # the container is real; only the host's free-space call fails, and the
        # truth that survives is "unknown", never a friendly zero
        tmp = tempfile.mkdtemp(prefix="saituls-cap-unk-")
        path = os.path.join(tmp, "c.vhdx")
        with open(path, "wb") as handle:
            handle.write(b"\0" * (7 * 1024 * 1024))
        try:
            with mock.patch.object(sa_capacity.shutil, "disk_usage",
                                   side_effect=OSError(21, "volume not ready")):
                facts = sa_capacity.container_facts(path, 16)
            self.assertTrue(facts["exists"])
            self.assertIsNone(facts["host_free_gb"])
            self.assertIn("host_free_unknown", sa_capacity.classify(facts))
            # capacity still must not be claimed: the header is not a VHDX
            self.assertFalse(facts["format_valid"])
        finally:
            os.remove(path)
            os.rmdir(tmp)

    def test_host_full_blocks_growth(self):
        facts = sa_capacity.container_facts(J(["C:", "nope", "x.vhdx"]), 16)
        facts.update({"exists": True, "file_length_gb": 1.0, "host_free_gb": 0.5,
                      "host_used_ratio": 0.99, "format_valid": True,
                      "host_volume": "C:", "virtual_capacity_gb": 16.0})
        self.assertIn("grow_blocked_host_full", sa_capacity.classify(facts))

    def test_high_usage_warns(self):
        facts = sa_capacity.container_facts(J(["C:", "nope", "x.vhdx"]), 16)
        facts.update({"exists": True, "file_length_gb": 1.0, "host_free_gb": 4.0,
                      "host_used_ratio": 0.95, "format_valid": True,
                      "host_volume": "C:", "virtual_capacity_gb": 16.0})
        self.assertIn("host_space_warning", sa_capacity.classify(facts))


@_NEEDS_VHDX
class RealVhdxMeasurementTests(unittest.TestCase):
    """Measured behaviour against a real dynamically expanding VHDX."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="saituls-cap-")
        cls.path = os.path.join(cls.tmp, "vault.vhdx")
        sa_win.create_vhdx(cls.path, 16 * 1024 ** 3)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_apparent_capacity_is_not_the_file_length(self):
        facts = sa_capacity.container_facts(self.path, 16)
        # a 16 GiB dynamic VHDX is a few MiB on disk; reporting the file length
        # as capacity is the defect this ticket exists to remove
        self.assertEqual(facts["virtual_capacity_gb"], 16.0)
        self.assertLess(facts["file_length_gb"], 1.0)
        self.assertNotEqual(facts["file_length_gb"], facts["virtual_capacity_gb"])
        self.assertLess(facts["allocated_gb"], 1.0)

    def test_measured_facts_carry_provenance(self):
        facts = sa_capacity.container_facts(self.path, 16)
        self.assertIn("VirtualSize", facts["sources"]["virtual_capacity_gb"])
        self.assertIn("st_size", facts["sources"]["file_length_gb"])
        self.assertIn("shutil.disk_usage", facts["sources"]["host_free_gb"])

    def test_host_volume_and_free_space_are_really_measured(self):
        facts = sa_capacity.container_facts(self.path, 16)
        self.assertIsNotNone(facts["host_volume"])
        self.assertTrue(facts["host_volume"].endswith(":"))
        self.assertIsNotNone(facts["host_free_gb"])
        self.assertGreater(facts["host_free_gb"], 0)
        self.assertIsNotNone(facts["host_used_ratio"])
        self.assertNotIn("host_volume_unknown", sa_capacity.classify(facts))

    def test_dynamic_vhdx_is_reported_expandable(self):
        facts = sa_capacity.container_facts(self.path, 16)
        self.assertTrue(facts["is_expandable"])

    def test_internal_filesystem_is_never_guessed(self):
        facts = sa_capacity.container_facts(self.path, 16)
        self.assertIsNone(facts["internal_total_gb"])
        self.assertIsNone(facts["internal_free_gb"])
        self.assertIsNone(facts["internal_used_gb"])

    def test_fixed_allocation_is_reported_unexpandable(self):
        # virtdisk's reply is substituted so the fixed-allocation *decision* is
        # exercised without a Hyper-V New-VHD; the decision function is the
        # subject, not the container
        fixed = {"virtual_size_bytes": 16 * 1024 ** 3,
                 "physical_size_bytes": 16 * 1024 ** 3,
                 "block_size_bytes": 32 * 1024 ** 2, "sector_size_bytes": 512,
                 "is_loaded": False}
        with mock.patch.object(sa_win, "virtual_disk_info", return_value=fixed):
            facts = sa_capacity.container_facts(self.path, 16)
        self.assertFalse(facts["is_expandable"])

    def test_attached_state_is_read_for_real(self):
        state = sa_capacity.attached_state(self.path)
        self.assertIs(state["loaded"], False)
        self.assertIn("IS_LOADED", state["source"])

    def test_provisioned_size_is_reported_as_configuration(self):
        facts = sa_capacity.container_facts(self.path, 16)
        self.assertEqual(facts["provisioned_gb"], 16)
        self.assertIn("configured", facts["provisioned_source"])


@_NEEDS_VHDX
class GrowPlanTests(unittest.TestCase):
    """The plan must be reachable when everything is true and refuse otherwise."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="saituls-cap-plan-")
        cls.path = os.path.join(cls.tmp, "vault.vhdx")
        sa_win.create_vhdx(cls.path, 16 * 1024 ** 3)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_plan_is_produced_and_names_the_real_operation(self):
        facts = sa_capacity.container_facts(self.path, 16)
        if facts["host_free_gb"] < 4:
            self.skipTest("host volume is too full for a +1 GiB plan")
        plan, refusal = sa_capacity.plan_grow(
            self.path, 16, 17, facts=facts, toolchain=dict(_TOOLCHAIN_READY))
        self.assertIsNone(refusal)
        self.assertEqual(plan["delta_gb"], 1)
        self.assertEqual(plan["measured_capacity_gb"], 16.0)
        self.assertIn("Resize-VHD", plan["command"])
        self.assertNotIn("Resize-VirtualDisk", plan["command"])
        steps = [step["step"] for step in plan["steps"]]
        self.assertEqual(steps, ["verify_idle", "confirm_detached", "resize_vhd",
                                 "verify_disk_size", "unlock_and_extend_fs",
                                 "verify"])
        self.assertTrue(all(check["ok"] for check in plan["checks"]))
        self.assertTrue(plan["consent_required"])
        self.assertEqual(plan["steps"][0]["requires"], "operator")
        self.assertTrue(plan["steps"][0]["consent"])

    def test_plan_is_never_produced_for_resize_virtualdisk(self):
        self.assertNotIn("Resize-VirtualDisk", sa_capacity.resize_vhd_command(32))
        self.assertIn("Resize-VHD", sa_capacity.resize_vhd_command(32))

    def test_target_below_measured_capacity_is_refused(self):
        facts = sa_capacity.container_facts(self.path, 16)
        plan, refusal = sa_capacity.plan_grow(
            self.path, 16, 16, facts=facts, toolchain=dict(_TOOLCHAIN_READY))
        self.assertIsNone(plan)
        self.assertIn("must exceed", refusal)

    def test_nonexistent_target_beyond_capacity_refused_by_measured_baseline(self):
        facts = sa_capacity.container_facts(self.path, 16)
        # provisioned says 32 but the measured VHDX is 16: the measurement wins
        facts["provisioned_gb"] = 32
        plan, refusal = sa_capacity.plan_grow(
            self.path, 32, 20, facts=facts, toolchain=dict(_TOOLCHAIN_READY))
        self.assertIsNone(plan)
        self.assertIn("must exceed provisioned", refusal)

    def test_insufficient_free_space_is_refused(self):
        facts = sa_capacity.container_facts(self.path, 16)
        facts["host_free_gb"] = 1.0
        plan, refusal = sa_capacity.plan_grow(
            self.path, 16, 32, facts=facts, toolchain=dict(_TOOLCHAIN_READY))
        self.assertIsNone(plan)
        self.assertIn("delta plus", refusal)

    def test_unknown_host_free_space_refuses_growth(self):
        shrinking = dict(sa_capacity.container_facts(self.path, 16))
        shrinking["host_free_gb"] = None
        plan, refusal = sa_capacity.plan_grow(
            self.path, 16, 32, facts=shrinking, toolchain=dict(_TOOLCHAIN_READY))
        self.assertIsNone(plan)
        self.assertIn("could not be verified", refusal)

    def test_attached_container_refuses_growth(self):
        facts = sa_capacity.container_facts(self.path, 16)
        plan, refusal = sa_capacity.plan_grow(
            self.path, 16, 17, facts=facts, toolchain=dict(_TOOLCHAIN_READY),
            attached={"loaded": True, "attached_to": "host", "source": "test"})
        self.assertIsNone(plan)
        self.assertIn("attached", refusal)

    def test_unverifiable_attachment_state_refuses_growth(self):
        facts = sa_capacity.container_facts(self.path, 16)
        plan, refusal = sa_capacity.plan_grow(
            self.path, 16, 17, facts=facts, toolchain=dict(_TOOLCHAIN_READY),
            attached={"loaded": None, "source": None, "detail": "probe failed"})
        self.assertIsNone(plan)
        self.assertIn("could not be verified", refusal)

    def test_unavailable_toolchain_refuses_growth_on_this_host(self):
        toolchain = sa_capacity.hyperv_toolchain(use_cache=False)
        if toolchain["available"]:
            self.skipTest("Hyper-V Resize-VHD is present on this host")
        facts = sa_capacity.container_facts(self.path, 16)
        plan, refusal = sa_capacity.plan_grow(self.path, 16, 17, facts=facts,
                                              toolchain=toolchain)
        self.assertIsNone(plan)
        self.assertIn("Resize-VHD", refusal)

    def test_hyperv_toolchain_probe_reports_its_facts(self):
        toolchain = sa_capacity.hyperv_toolchain(use_cache=False)
        self.assertIn("available", toolchain)
        self.assertEqual(toolchain["available"], bool(toolchain["resize_vhd"]))
        if not toolchain["available"]:
            self.assertTrue(toolchain["detail"])


class ExecuteGrowTests(unittest.TestCase):
    """Even a fully consented grow refuses without a passed plan and a lock."""

    def test_execute_refuses_without_consent(self):
        result = sa_capacity.execute_grow(J(["C:", "x.vhdx"]), 16, 32)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], sa_capacity.RETURN_REFUSED)
        self.assertIn("consent", result["detail"])

    def test_execute_refuses_without_idle_confirmation(self):
        result = sa_capacity.execute_grow(J(["C:", "x.vhdx"]), 16, 32,
                                          consent=True)
        self.assertFalse(result["ok"])
        self.assertIn("idle", result["detail"])

    def test_execute_refuses_when_the_plan_does(self):
        result = sa_capacity.execute_grow(J(["C:", "x.vhdx"]), 16, 32,
                                          consent=True, idle_confirmed=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], sa_capacity.RETURN_BLOCKED)
        # whatever the reason, nothing ran
        self.assertEqual(
            [stage for stage in result["stages"]], [])
        self.assertEqual(result["journal"][-1]["stage"], "plan")

    def test_execute_refuses_when_the_broker_state_is_unknown(self):
        journal = []
        result = sa_capacity.execute_grow(
            J(["C:", "x.vhdx"]), 16, 32, consent=True, idle_confirmed=True,
            facts={"exists": True, "format_valid": True,
                   "virtual_capacity_gb": 16.0, "host_free_gb": 100.0},
            toolchain=dict(_TOOLCHAIN_READY),
            attached={"loaded": False, "source": "test"},
            broker_locked=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], sa_capacity.RETURN_BLOCKED)
        self.assertIn("broker", result["detail"])
        journal = result["journal"]
        self.assertEqual(journal[-1]["stage"], "exclusive_admission")


class CapacityCliTests(unittest.TestCase):

    def _run(self, argv, profile):
        parser = sa_cli.build_parser()
        args = parser.parse_args(argv)
        buffer = io.StringIO()
        with mock.patch.object(sa_cli, "load") as load:
            load.return_value.get.return_value = profile
            with contextlib.redirect_stdout(buffer):
                code = sa_cli.cmd_capacity(args)
        return code, json.loads(buffer.getvalue())

    def test_cli_reports_unknown_profile(self):
        parser = sa_cli.build_parser()
        args = parser.parse_args(["capacity", "ghost"])
        with mock.patch.object(sa_cli, "load") as load:
            load.return_value.get.side_effect = sa_cli.sa_config.ConfigError(
                "unknown profile id: 'ghost'")
            code = sa_cli.cmd_capacity(args)
        self.assertEqual(code, sa_cli.EXIT_CONFIG)

    def test_omitted_grow_size_is_a_read_only_probe(self):
        profile = mock.Mock(container=J(["C:", "nope", "x.vhdx"]), size_gb=16)
        code, payload = self._run(["capacity", "obsidian"], profile)
        self.assertEqual(code, sa_cli.EXIT_OK)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["plan"])
        self.assertIsNone(payload["refusal"])

    def test_explicit_zero_target_is_an_error_not_a_read_only_success(self):
        profile = mock.Mock(container=J(["C:", "nope", "x.vhdx"]), size_gb=16)
        code, payload = self._run(["capacity", "obsidian", "--grow-gb", "0"],
                                  profile)
        self.assertEqual(code, sa_cli.EXIT_FAILED)
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["plan"])
        self.assertIn("greater than zero", payload["refusal"])

    def test_negative_target_is_an_error(self):
        profile = mock.Mock(container=J(["C:", "nope", "x.vhdx"]), size_gb=16)
        code, payload = self._run(["capacity", "obsidian", "--grow-gb=-4"],
                                  profile)
        self.assertEqual(code, sa_cli.EXIT_FAILED)
        self.assertIn("greater than zero", payload["refusal"])


if __name__ == "__main__":
    unittest.main()
