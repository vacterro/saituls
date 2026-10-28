"""T-254: the emergency recovery steps refuse, stop and tell the truth.

The printed emergency procedure is already correct. These tests cover what
the scripted version must guarantee before an operator in distress trusts
it with their only copy of a vault key:

  * a missing container is MISSING, not an attempt to mount nothing;
  * an unknown profile id is refused before any Windows tooling runs;
  * a malformed recovery password is refused without any subprocess;
  * a refusal anywhere stops the sequence -- no step ever runs past one;
  * no step output ever contains the recovery password.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Scripts" / "secure_apps"))

import sa_cli          # noqa: E402
import sa_recovery     # noqa: E402
import sa_config       # noqa: E402

KEY = "111111-222222-333333-444444-555555-666666-777777-888888"


class StopOnRefusalTests(unittest.TestCase):
    """The sequence is a chain of gates, not a script that blindly continues."""

    def test_missing_container_is_missing_and_stops(self):
        sequence, verdict = sa_recovery.run_recovery_sequence(
            r"C:\definitely\not\here.vhdx", KEY)
        self.assertEqual([s[0] for s in sequence], ["attach"])
        self.assertEqual(sequence[0][1], sa_recovery.RETURN_MISSING)
        self.assertEqual(verdict, "stopped")
        self.assertIn("container not found", sequence[0][2][0])

    def test_unattachable_image_refuses_and_never_unlocks(self):
        with mock.patch.object(sa_recovery, "attach_with_letter",
                               return_value=(sa_recovery.RETURN_REFUSED,
                                             None, ["mount refused"])):
            sequence, verdict = sa_recovery.run_recovery_sequence(
                "x.vhdx", KEY)
        self.assertEqual([s[0] for s in sequence], ["attach"])
        self.assertEqual(verdict, "stopped")

    def test_unlock_refusal_stops_before_lock(self):
        with mock.patch.object(
                sa_recovery, "attach_with_letter",
                return_value=(sa_recovery.RETURN_OK, {"letter": "Z"}, [])), \
             mock.patch.object(
                 sa_recovery, "unlock_with_password",
                 return_value=(sa_recovery.RETURN_REFUSED, None,
                               ["wrong key"])) as unlock, \
             mock.patch.object(sa_recovery, "lock_and_dismount") as close:
            sequence, verdict = sa_recovery.run_recovery_sequence(
                "x.vhdx", KEY)
        unlock.assert_called_once()
        close.assert_not_called()
        self.assertEqual([s[0] for s in sequence], ["attach", "unlock"])
        self.assertEqual(verdict, "stopped")

    def test_malformed_password_refused_without_subprocess(self):
        with mock.patch.object(sa_recovery.subprocess, "run") as run:
            status, _data, detail = sa_recovery.unlock_with_password(
                "x.vhdx", "Z", "not-a-key")
        run.assert_not_called()
        self.assertEqual(status, sa_recovery.RETURN_REFUSED)
        self.assertIn("refused", detail[0])

    def test_empty_password_refused(self):
        status, _data, detail = sa_recovery.unlock_with_password("x.vhdx", "Z", "")
        self.assertEqual(status, sa_recovery.RETURN_REFUSED)

    def test_cli_rejects_unknown_profile_before_any_tooling(self):
        parser = sa_cli.build_parser()
        args = parser.parse_args(["emergency", "no-such-profile", "attach"])
        with mock.patch.object(sa_recovery, "attach_with_letter") as attach:
            code = sa_cli.cmd_emergency(args)
        attach.assert_not_called()
        self.assertEqual(code, sa_cli.EXIT_CONFIG)

    def test_cli_exit_codes_are_truthful(self):
        parser = sa_cli.build_parser()
        for step, patched, status, expected in [
            ("attach", "attach_with_letter", sa_recovery.RETURN_REFUSED,
             sa_cli.EXIT_REFUSED),
            ("attach", "attach_with_letter", sa_recovery.RETURN_UNKNOWN,
             sa_cli.EXIT_FAILED),
            ("close", "lock_and_dismount", sa_recovery.RETURN_OK,
             sa_cli.EXIT_OK),
        ]:
            with self.subTest(step=step, status=status):
                args = parser.parse_args(
                    ["emergency", "prof", step, "--letter", "Z"])
                with mock.patch.object(sa_cli.sa_config, "load_registry") as load:
                    load.return_value.get.return_value.container = "x.vhdx"
                    with mock.patch("os.path.isfile", return_value=True), \
                         mock.patch.object(sa_recovery, patched,
                                           return_value=(status, {}, [])):
                        code = sa_cli.cmd_emergency(args)
                self.assertEqual(code, expected)

    def test_no_step_output_ever_contains_the_password(self):
        # A subprocess caputure that echoes everything it was handed must
        # still come back clean: the key is checked, never trusted.
        captured = []

        def fake_run(cmd, capture_output, timeout, input=None, env=None, **kwargs):
            captured.append(cmd)
            echoed = (input.decode(errors="replace") if isinstance(input, bytes) else (input or "")) or (env or {}).get(
                "SAITULS_RECOVERY_KEY", "")
            return mock.Mock(returncode=1,
                             stdout=("echoed " + echoed).encode(),
                             stderr=b"")

        with mock.patch.object(sa_recovery.subprocess, "run", fake_run):
            status, _data, detail = sa_recovery.unlock_with_password(
                "x.vhdx", "Z", KEY)
        self.assertEqual(status, sa_recovery.RETURN_REFUSED)
        blob = repr(status) + repr(detail)
        self.assertNotIn(KEY, blob)
        self.assertNotIn("111111", blob)


if __name__ == "__main__":
    unittest.main()
