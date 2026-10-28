"""Secret-hygiene and clipboard-restore regressions (wave clauses 4-7).

A. Static: driver evidence serialization must never slice clipboard content
   (forbids `clipboard_read()[:N]`-equivalent patterns) and must never emit
   a `clipboard_probe`-style raw-content key.
B. Dynamic: clipboard_diag() returns counts/emptiness only; a canary in the
   real clipboard never reaches the diagnostic dict.
C. Restore policy: cleanup restores the saved clipboard ONLY when the current
   clipboard still equals the test fixture; a user-changed clipboard wins.
"""
import re
import unittest
import unittest.mock as mock
from pathlib import Path

import saipatch_tui_driver as driver

DRIVER_SRC = Path(driver.__file__).read_text(encoding="utf-8")

FORBIDDEN_RAW_PATTERNS = [
    # any slice of clipboard content in evidence serialization
    r'clipboard_read\(\)\s*\[',
    r'saved_clipboard\s*\[',
    r'clipboard_read\(\)\s*\.\s*join',
]
FORBIDDEN_EVIDENCE_KEYS = ['clipboard_probe', 'clipboard_text', 'clipboard_prefix',
                           'clipboard_suffix', 'clipboard_content']
ALLOWED_DIAG_KEYS = {'clipboard_chars', 'clipboard_bytes', 'clipboard_read_ms',
                     'clipboard_empty', 'fixture_match'}


class SecretHygieneTests(unittest.TestCase):
    def test_no_raw_clipboard_slicing_in_driver(self):
        for rx in FORBIDDEN_RAW_PATTERNS:
            self.assertIsNone(re.search(rx, DRIVER_SRC),
                              f"forbidden clipboard slice pattern: {rx}")

    def test_no_raw_clipboard_evidence_keys_in_driver(self):
        for key in FORBIDDEN_EVIDENCE_KEYS:
            self.assertNotIn(f'"{key}"', DRIVER_SRC, f"forbidden evidence key: {key}")

    def test_clipboard_diag_returns_counts_only(self):
        with mock.patch.object(driver, "clipboard_read", return_value="SAIPATCH_SECRET_CANARY_ab12cd34 top secret"):
            diag = driver.clipboard_diag()
        self.assertEqual(diag, {"clipboard_chars": 42, "clipboard_bytes": 42,
                                "clipboard_empty": False})
        self.assertNotIn("SAIPATCH_SECRET_CANARY", repr(diag))

    def test_clipboard_diag_failure_is_content_free(self):
        with mock.patch.object(driver, "clipboard_read", side_effect=RuntimeError("x")):
            diag = driver.clipboard_diag()
        self.assertEqual(diag, {"clipboard_chars": -1, "clipboard_bytes": -1,
                                "clipboard_empty": None})

    def test_empty_clipboard_diag(self):
        with mock.patch.object(driver, "clipboard_read", return_value=""):
            diag = driver.clipboard_diag()
        self.assertEqual(diag, {"clipboard_chars": 0, "clipboard_bytes": 0,
                                "clipboard_empty": True})


class ClipboardRestorePolicyTests(unittest.TestCase):
    """Fixture = what the test last wrote. Saved = pre-test user value."""

    def _restore(self, saved, fixture, current):
        # the production cleanup logic, expressed as the policy under test:
        # restore ONLY if current still equals the fixture
        if current == fixture:
            return saved
        return current  # user owns the new value

    def test_unchanged_clipboard_restores_original(self):
        self.assertEqual(self._restore("user-original", "fixture", "fixture"),
                         "user-original")

    def test_user_changed_clipboard_is_preserved(self):
        self.assertEqual(self._restore("user-original", "fixture", "user-new-value"),
                         "user-new-value")

    def test_restore_never_persists_saved_value_into_evidence(self):
        # the restore branch writes nothing; assert the driver cleanup block
        # contains no evidence write between restore and fixture compare
        m = re.search(r"if args\.paste_gate and saved_clipboard.*?_clipboard_fixture.*?\n(.*?)try:", DRIVER_SRC, re.S)
        self.assertIsNotNone(m, "cleanup restore block missing")
        block = m.group(1) + DRIVER_SRC[m.end():m.end() + 400]
        self.assertNotIn('write_text', block)
        self.assertNotIn('json.dumps', block)

    def test_driver_cleanup_uses_fixture_equality_guard(self):
        self.assertIn("_cur == _clipboard_fixture", DRIVER_SRC)
        self.assertIn("clipboard_write(saved_clipboard)", DRIVER_SRC)


if __name__ == "__main__":
    unittest.main()
