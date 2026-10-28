"""The UI and recovery-material promises, tested rather than asserted.

Acceptance criteria 7, 8 and 9 are all statements about what the interface
tells the operator, so they are tested at the level of the text and the state
that produces it:

  * AC7 -- no state in which the product claims full protection while a
    readable plaintext copy exists;
  * AC8 -- the recovery password does not sit on the clipboard indefinitely,
    and is never wiped out from under whatever the user copied next;
  * AC9 -- acknowledging recovery proves the operator captured the key, which
    a phrase typed from the source code does not.

These are unit tests over module-level logic. The dialogs need a Qt event loop
and a display, so what is asserted here is the decision each dialog makes, not
its pixels.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Scripts" / "secure_apps"))

import sa_applife  # noqa: E402
import sa_migrate  # noqa: E402
import sa_setup  # noqa: E402

from test_secure_apps import BaseCase  # noqa: E402

KEY = ("111111-222222-333333-444444-555555-666666-777777-888888")


class PlaintextTruthTests(BaseCase):
    """AC7: the migration verdict never outruns the filesystem."""

    def _journal(self, h, archive, last_step=sa_migrate.STEP_COMPLETE):
        """A completed migration journal at the path the product really reads.

        Writing anywhere else would test a file no code path ever opens: the
        checklist asks ``<state_dir>/migration-<profile_id>.json``.
        """
        state_dir = h.registry.state_dir
        os.makedirs(state_dir, exist_ok=True)
        path = Path(state_dir) / "migration-vault.json"
        path.write_text(
            '{"schema": "saituls.secure-apps.migration/1", '
            '"schema_version": 1, "profile_id": "vault", '
            '"source": "C:/vaults/plain", "plaintext_archive": "%s", '
            '"final_mount": "C:/vaults/final", "last_step": "%s"}'
            % (str(archive).replace("\\", "/"), last_step),
            encoding="utf-8")
        return str(path)

    def test_a_present_archive_means_protection_is_incomplete(self):
        h = self.harness()
        archive = Path(h.tmp) / "vault.plaintext-20260101-000000"
        (archive / "notes").mkdir(parents=True)
        (archive / "notes" / "a.md").write_text("hi", encoding="utf-8")
        state = sa_migrate.inspect(self._journal(h, archive))
        self.assertTrue(state["plaintext_copy_remains"])
        self.assertFalse(state["full_protection_complete"])

    def test_an_emptied_archive_means_protection_is_complete(self):
        h = self.harness()
        archive = Path(h.tmp) / "vault.plaintext-20260101-000000"
        archive.mkdir(parents=True)
        state = sa_migrate.inspect(self._journal(h, archive))
        self.assertFalse(state["plaintext_copy_remains"])
        self.assertTrue(state["full_protection_complete"])

    def test_an_interrupted_migration_is_never_full_protection(self):
        h = self.harness()
        archive = Path(h.tmp) / "vault.plaintext-20260101-000000"
        archive.mkdir(parents=True)
        state = sa_migrate.inspect(
            self._journal(h, archive, sa_migrate.STEP_CUTOVER_BEGIN))
        self.assertFalse(state["full_protection_complete"])

    def test_checklist_step9_is_action_required_while_the_copy_exists(self):
        """Step 8 going green must not launder step 9 into a footnote."""
        h = self.harness()
        archive = Path(h.tmp) / "vault.plaintext-20260101-000000"
        archive.mkdir(parents=True)
        (archive / "note.md").write_text("the operator's own words",
                                         encoding="utf-8")
        # Steps 1-7 are driven to COMPLETE so step 9 is reached on its own
        # merits rather than because an earlier step waited.
        journal_path = self._journal(h, archive)
        verdict = sa_migrate.inspect(str(journal_path))["verdict"]
        checklist = sa_setup.get_setup_checklist(
            h.registry, profile_id="vault",
            preflight_report=_all_gates_pass(h),
            commissioned=True, enrolled_fact=True,
            acceptance_verdict=_acceptance_ok(),
            migration_verdict=verdict)
        step8 = next(s for s in checklist if s["index"] == 8)
        step9 = next(s for s in checklist if s["index"] == 9)
        self.assertEqual(step8["status"], sa_setup.STEP_COMPLETE,
                         "the migration itself is genuinely complete")
        self.assertNotIn("protected", step8["summary"].lower())
        self.assertEqual(step9["status"], sa_setup.STEP_ACTION_REQUIRED)
        self.assertIn(str(archive).replace("\\", "/"), step9["summary"])
        self.assertIn("NOT protected", step9["summary"])

    def test_step9_is_complete_once_the_copy_is_gone(self):
        h = self.harness()
        archive = Path(h.tmp) / "vault.plaintext-20260101-000000"
        archive.mkdir(parents=True)
        journal_path = self._journal(h, archive)
        # The operator moved the copy away themselves; nothing else changed.
        archive.rmdir()
        checklist = sa_setup.get_setup_checklist(
            h.registry, profile_id="vault",
            preflight_report=_all_gates_pass(h),
            commissioned=True, enrolled_fact=True,
            acceptance_verdict=_acceptance_ok(),
            migration_verdict="COMPLETE")
        step9 = next(s for s in checklist if s["index"] == 9)
        self.assertEqual(step9["status"], sa_setup.STEP_COMPLETE)

    def test_a_caller_supplied_verdict_does_not_hide_a_copy_on_disk(self):
        """The GUI passes a verdict it read earlier; the disk is still asked."""
        h = self.harness()
        archive = Path(h.tmp) / "vault.plaintext-20260101-000000"
        archive.mkdir(parents=True)
        (archive / "note.md").write_text("secret", encoding="utf-8")
        self._journal(h, archive)
        checklist = sa_setup.get_setup_checklist(
            h.registry, profile_id="vault",
            preflight_report=_all_gates_pass(h),
            commissioned=True, enrolled_fact=True,
            acceptance_verdict=_acceptance_ok(),
            migration_verdict="COMPLETE")
        step9 = next(s for s in checklist if s["index"] == 9)
        self.assertEqual(step9["status"], sa_setup.STEP_ACTION_REQUIRED)

    def test_setup_is_not_ready_while_the_copy_exists(self):
        h = self.harness()
        archive = Path(h.tmp) / "vault.plaintext-20260101-000000"
        archive.mkdir(parents=True)
        (archive / "note.md").write_text("plaintext", encoding="utf-8")
        journal_path = self._journal(h, archive)
        state = sa_migrate.inspect(journal_path)
        action = sa_setup.get_next_setup_action(
            h.registry, "vault",
            preflight_report=_all_gates_pass(h),
            commissioned=True, enrolled_fact=True,
            acceptance_verdict=_acceptance_ok(),
            migration_verdict=state["verdict"],
            migration_plaintext_remains=state["plaintext_copy_remains"])
        self.assertEqual(action, sa_setup.ACTION_SECURE_PLAINTEXT_BACKUP)


class RecoveryClipboardTests(unittest.TestCase):
    """AC8: bounded, conditional clipboard lifetime for recovery material."""

    def test_clear_only_touches_the_clipboard_when_it_holds_this_key(self):
        from secure_apps_gui import RecoveryDialog
        dialog = RecoveryDialog.__new__(RecoveryDialog)
        dialog.recovery_text = KEY

        class Clip(object):
            def __init__(self, text):
                self.value = text
                self.cleared = 0

            def text(self):
                return self.value

            def clear(self):
                self.cleared += 1
                self.value = ""

        # The user copied the key: it gets cleared.
        holding_key = Clip(KEY)
        with _patched_clipboard(holding_key):
            self.assertTrue(RecoveryDialog._clear_clipboard(dialog))
        self.assertEqual(holding_key.cleared, 1)

        # The user copied something else since: the clipboard is left alone.
        holding_other = Clip("an innocent shopping list")
        with _patched_clipboard(holding_other):
            self.assertFalse(RecoveryDialog._clear_clipboard(dialog))
        self.assertEqual(holding_other.cleared, 0)
        self.assertEqual(holding_other.value, "an innocent shopping list")

    def test_the_button_states_a_bounded_lifetime_not_an_open_ended_copy(self):
        from secure_apps_gui import RecoveryDialog
        label = [button for button in _recovery_dialog_buttons()
                 if "opy" in button][0]
        self.assertIn("30", label)
        self.assertGreater(RecoveryDialog.CLIPBOARD_LIFETIME_MS, 0)
        self.assertLessEqual(RecoveryDialog.CLIPBOARD_LIFETIME_MS, 120_000,
                             "a recovery secret must not outlive a minute or two "
                             "on a shared clipboard")


class RecoveryChallengeTests(unittest.TestCase):
    """AC9: the acknowledgement has to prove the key was captured."""

    def _dialog(self):
        from secure_apps_gui import RecoveryDialog
        dialog = RecoveryDialog.__new__(RecoveryDialog)
        dialog.recovery_text = KEY
        dialog._challenge_indices = [0, 7]
        dialog.challenge_hint = _StubLabel()
        dialog.challenge_boxes = [_StubBox(""), _StubBox("")]
        dialog.entry = _StubLineEdit()
        return dialog

    def test_the_challenge_asks_for_groups_of_the_key_itself(self):
        dialog = self._dialog()
        self.assertEqual(dialog._key_groups()[0], "111111")
        self.assertEqual(dialog._key_groups()[7], "888888")
        self.assertEqual(dialog._challenge_answered.__name__, "_challenge_answered")

    def test_the_right_groups_are_accepted(self):
        dialog = self._dialog()
        dialog.challenge_boxes = [_StubBox("111111"), _StubBox("888888")]
        self.assertTrue(dialog._challenge_answered())
        self.assertEqual([box.text() for box in dialog.challenge_boxes], ["", ""],
                         "the typed groups must be wiped whatever the verdict")

    def test_a_wrong_group_is_refused_and_wiped(self):
        dialog = self._dialog()
        dialog.challenge_boxes = [_StubBox("111111"), _StubBox("000000")]
        self.assertFalse(dialog._challenge_answered())
        self.assertEqual([box.text() for box in dialog.challenge_boxes], ["", ""])

    def test_case_and_spacing_do_not_lock_the_owner_out(self):
        dialog = self._dialog()
        dialog.challenge_boxes = [_StubBox(" 111111 "), _StubBox("888888")]
        self.assertTrue(dialog._challenge_answered())

    def test_the_challenge_positions_are_not_fixed(self):
        """A fixed position is a fixed answer, published in this file."""
        import secrets as _secrets
        dialog = self._dialog()
        dialog.challenge_boxes = [_StubBox(""), _StubBox("")]
        seen = set()
        for _ in range(40):
            dialog._new_challenge()
            seen.add(tuple(dialog._challenge_indices))
        self.assertGreater(len(seen), 1,
                           "the challenge must not be the same every time")
        self.assertTrue(all(len(pair) == 2 for pair in seen))

    def test_a_key_without_groups_still_lets_the_owner_through(self):
        """Refusing someone their own vault over a formatting quirk is worse."""
        dialog = self._dialog()
        dialog.recovery_text = "OPAQUERECOVERYBLOB"  # no groups to ask for
        dialog._new_challenge()
        self.assertEqual(dialog._challenge_indices, [])
        self.assertTrue(dialog._challenge_answered())


class SingleInstanceRefusalTests(unittest.TestCase):
    """AC10: a pre-existing unmanaged instance is refused, never adopted."""

    def test_conflicts_exclude_our_own_family(self):
        adapter = sa_applife.FakeProcessAdapter()
        supervisor = sa_applife.AppSupervisor(adapter=adapter)
        ours = adapter.spawn("obsidian.exe", [], ".")
        supervisor.attach(ours)
        with mock_process_names({ours: "obsidian.exe", 4242: "obsidian.exe"}):
            conflicts = supervisor.conflicting_instances("obsidian.exe")
        self.assertNotIn(ours, conflicts)
        self.assertIn(4242, conflicts)

    def test_no_conflict_when_nothing_else_runs(self):
        adapter = sa_applife.FakeProcessAdapter()
        supervisor = sa_applife.AppSupervisor(adapter=adapter)
        ours = adapter.spawn("obsidian.exe", [], ".")
        supervisor.attach(ours)
        with mock_process_names({ours: "obsidian.exe"}):
            self.assertEqual(supervisor.conflicting_instances("obsidian.exe"), [])

    def test_a_conflict_refuses_the_launch_instead_of_spawning(self):
        adapter = sa_applife.FakeProcessAdapter()
        supervisor = sa_applife.AppSupervisor(adapter=adapter)
        spawned = []
        adapter.spawn = lambda *a: spawned.append(a) or 9999
        with mock_process_names({4242: "obsidian.exe"}):
            with self.assertRaises(sa_applife.AppLaunchError) as ctx:
                supervisor.launch("C:/app/Obsidian.exe", ["C:/vault"], "C:/app")
        self.assertEqual(ctx.exception.category, "single_instance_conflict")
        self.assertEqual(spawned, [], "nothing may be spawned on a conflict")


# ── helpers ───────────────────────────────────────────────────────────────
def _all_gates_pass(h):
    # Booleans, because sa_setup compares these with `is True` / `is False` --
    # the same shape sa_privtask.preflight really produces.
    return {
        "EnableLUA": True, "broker_elevated": False, "broker_integrity": "Medium",
        "scheduled_helper_installed": True,
        "scheduled_helper_definition_valid": True,
    }


def _acceptance_ok():
    class Verdict(object):
        ok = True
        record = {"silent_privileged_start_accepted": True,
                  "verdict": "PASS", "all_checks_passed": True}
    return Verdict()


class _StubBox(object):
    def __init__(self, value):
        self.value = value

    def text(self):
        return self.value

    def clear(self):
        self.value = ""


class _StubLabel(object):
    def setText(self, _text):
        pass


class _StubLineEdit(object):
    def __init__(self):
        self.value = ""

    def text(self):
        return self.value

    def clear(self):
        self.value = ""


def _recovery_dialog_buttons():
    """The real button labels, found without needing a display.

    Parsed out of the class body with ast rather than a line scan: a button is
    assigned to a name, so the literal is never at the start of the line.
    """
    import ast
    import secure_apps_gui as gui
    tree = ast.parse(Path(gui.__file__).read_text(encoding="utf-8"))
    dialog = next(node for node in tree.body
                  if isinstance(node, ast.ClassDef)
                  and node.name == "RecoveryDialog")
    found = []
    for node in ast.walk(dialog):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", getattr(node.func, "id", ""))
                    == "QPushButton"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            found.append(node.args[0].value)
    return found


class _patched_clipboard(object):
    def __init__(self, clipboard):
        self.clipboard = clipboard

    def __enter__(self):
        import secure_apps_gui as gui
        self._original = gui.QApplication.clipboard
        gui.QApplication.clipboard = staticmethod(lambda: self.clipboard)
        return self.clipboard

    def __exit__(self, *exc):
        import secure_apps_gui as gui
        gui.QApplication.clipboard = self._original
        return False


def mock_process_names(mapping):
    """Feed a fixed pid -> image-name table to psutil.process_iter."""
    import unittest.mock as mock

    class Proc(object):
        def __init__(self, pid, name):
            self.info = {"pid": pid, "name": name}

    return mock.patch("psutil.process_iter",
                      return_value=[Proc(pid, name)
                                    for pid, name in mapping.items()])


if __name__ == "__main__":
    unittest.main()
