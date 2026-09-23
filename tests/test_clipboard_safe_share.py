"""Comprehensive test suite for Clipboard+ Safe Share Sanitizer.

Verifies:
1. Plain prose remains byte-for-byte unchanged.
2. Ordinary public URL remains usable.
3. UTM/tracking parameters are removed.
4. Product-Hunt-style tracking redirect with a locally recoverable destination produces the safe destination.
5. Unresolvable personalized tracking redirect becomes [TRACKING LINK REMOVED].
6. unsubscribe URL with a token is fully neutralized.
7. password-reset / magic-login / verification URLs are neutralized.
8. token/access_token/session/code/sig/user_id/email values do not survive sanitization.
9. JWT and Authorization Bearer values are redacted.
10. PEM private-key body does not survive sanitization.
11. Markdown-visible labels survive where practical.
12. Unicode text remains correct.
13. Clipboard with image only is untouched by Safe Copy.
14. Explorer CF_HDROP/file-copy clipboard is untouched.
15. Ordinary Ctrl+C text is never automatically sanitized.
16. Existing image-monitor tests remain green.
17. No test output/log contains the fixture secret values after the sanitizer executes.
"""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock
import io
import time

# Dynamically import Scripts/saipatch/clipboard+.pyw
CLIPBOARD_PLUS_PATH = Path(__file__).resolve().parent.parent / "Scripts" / "saipatch" / "clipboard+.pyw"
spec = importlib.util.spec_from_file_location("clipboard_plus", CLIPBOARD_PLUS_PATH)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)


class ClipboardSafeShareTests(unittest.TestCase):

    def test_01_plain_prose_unchanged(self):
        text = "Hello world! Just some regular notes with no links or secrets.\nAnother line here."
        res = cp.sanitize_share_text(text)
        self.assertEqual(res.text, text)
        self.assertEqual(res.sanitized_text, text)
        self.assertFalse(res.modified)
        self.assertEqual(res.urls_changed, 0)
        self.assertEqual(res.urls_removed, 0)
        self.assertEqual(res.secrets_redacted, 0)

    def test_02_ordinary_public_url_usable(self):
        text = "Documentation is available at https://example.com/docs/intro and https://python.org."
        res = cp.sanitize_share_text(text)
        self.assertEqual(res.text, text)
        self.assertFalse(res.modified)
        self.assertEqual(res.urls_changed, 0)
        self.assertEqual(res.urls_removed, 0)

    def test_03_utm_and_tracking_params_removed(self):
        text = "Check out https://example.com/page?utm_source=twitter&utm_medium=social&gclid=12345&fbclid=abcdef&mc_cid=mc1&mc_eid=mc2&id=987."
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertEqual(res.urls_changed, 1)
        self.assertNotIn("utm_source", res.text)
        self.assertNotIn("gclid", res.text)
        self.assertNotIn("fbclid", res.text)
        self.assertNotIn("mc_cid", res.text)
        self.assertIn("https://example.com/page?id=987.", res.text)

    def test_04_product_hunt_tracking_redirect_recovered(self):
        text = "See product: https://link.producthunt.com/r?url=https%3A%2F%2Fmyapp.com%2Ffeatures%3Futm_source%3Dproducthunt"
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertEqual(res.urls_changed, 1)
        self.assertEqual(res.urls_removed, 0)
        self.assertIn("https://myapp.com/features", res.text)
        self.assertNotIn("producthunt", res.text)

    def test_05_unresolvable_tracking_redirect_removed(self):
        text = "Click to view: https://trk.mailservice.com/click/ab12cd34 and https://email.service.com/s-links/987xyz"
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertEqual(res.urls_removed, 2)
        self.assertNotIn("ab12cd34", res.text)
        self.assertNotIn("987xyz", res.text)
        self.assertEqual(res.text, "Click to view: [TRACKING LINK REMOVED] and [TRACKING LINK REMOVED]")

    def test_06_unsubscribe_url_neutralized(self):
        text = "To stop receiving emails, go to https://newsletter.com/unsubscribe?token=secret12345."
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertEqual(res.urls_removed, 1)
        self.assertNotIn("secret12345", res.text)
        self.assertIn("[LINK REMOVED].", res.text)

    def test_07_action_urls_neutralized(self):
        text = (
            "Links:\n"
            "Reset: https://auth.example.com/reset-password/abc12345\n"
            "Magic: https://auth.example.com/magic-login?key=fakeKey999\n"
            "Verify: https://auth.example.com/verify-email?code=code555\n"
            "Invite: https://app.example.com/invite/tok99999\n"
            "Account: https://example.com/account-recovery?id=123"
        )
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertEqual(res.urls_removed, 5)
        self.assertNotIn("abc12345", res.text)
        self.assertNotIn("fakeKey999", res.text)
        self.assertNotIn("code555", res.text)
        self.assertNotIn("tok99999", res.text)
        self.assertEqual(res.text.count("[LINK REMOVED]"), 5)

    def test_08_sensitive_query_parameters_do_not_survive(self):
        fake_token = "fakeTokenVal123"
        fake_access = "fakeAccessVal456"
        fake_session = "fakeSessionVal789"
        fake_code = "fakeCodeVal999"
        fake_sig = "fakeSigVal888"
        fake_uid = "fakeUserVal777"
        fake_email = "victim_secret@domain.com"

        text = f"https://api.example.com/data?token={fake_token}&access_token={fake_access}&session={fake_session}&code={fake_code}&sig={fake_sig}&user_id={fake_uid}&email={fake_email}&safe_param=hello"
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertIn("https://api.example.com/data?safe_param=hello", res.text)
        for secret in (fake_token, fake_access, fake_session, fake_code, fake_sig, fake_uid, fake_email):
            self.assertNotIn(secret, res.text)

    def test_09_jwt_and_bearer_redacted(self):
        fake_jwt1 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.fakeSignatureSecretPart1"
        fake_jwt2 = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyMTIzNDU2In0.fakeSignatureSecretPart2"
        text = f"Headers:\nAuthorization: Bearer {fake_jwt1}\n\nToken in body:\n{fake_jwt2}"
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertNotIn(fake_jwt1, res.text)
        self.assertNotIn(fake_jwt2, res.text)
        self.assertIn("Authorization: Bearer [REDACTED]", res.text)
        self.assertIn("[JWT REDACTED]", res.text)
        self.assertGreaterEqual(res.secrets_redacted, 2)

    def test_10_pem_private_key_redacted(self):
        pem_key = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Yfakefake123456789fakefakefake\n"
            "AbcDefGhIjKlMnOpQrStUvWxYz0123456789+/fakefake==\n"
            "-----END RSA PRIVATE KEY-----"
        )
        text = f"Here is the key:\n{pem_key}\nSave it."
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertNotIn("MIIEowIBAAKCAQEA0Yfakefake123456789fakefakefake", res.text)
        self.assertIn("[PRIVATE KEY REDACTED]", res.text)
        self.assertEqual(res.secrets_redacted, 1)

    def test_11_markdown_visible_labels_survive(self):
        text = (
            "Check our [Release Notes](https://example.com/releases?utm_source=slack) or "
            "[Reset Your Password](https://auth.example.com/reset-password?token=secret123) or "
            "[Product Link](https://trk.mail.com/click/abc123)."
        )
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertIn("[Release Notes](https://example.com/releases)", res.text)
        self.assertIn("[Reset Your Password]([LINK REMOVED])", res.text)
        self.assertIn("[Product Link]([TRACKING LINK REMOVED])", res.text)
        self.assertNotIn("secret123", res.text)
        self.assertNotIn("abc123", res.text)

    def test_12_unicode_text_correct(self):
        text = "Tere! Vaata siia: https://näide.ee/tee?utm_medium=postitus ja siia: https://example.com/search?q=тест&gclid=123 🚀"
        res = cp.sanitize_share_text(text)
        self.assertTrue(res.modified)
        self.assertIn("Tere! Vaata siia:", res.text)
        self.assertIn("https://näide.ee/tee", res.text)
        self.assertNotIn("utm_medium", res.text)
        self.assertIn("https://example.com/search?q=%D1%82%D0%B5%D1%81%D1%82", res.text)
        self.assertNotIn("gclid", res.text)
        self.assertIn("🚀", res.text)

    def test_13_clipboard_with_image_only_untouched(self):
        with patch.object(cp, "get_clipboard_text", return_value=None):
            with patch.object(cp, "set_clipboard_text") as mock_set:
                res = cp.safe_copy_clipboard()
                self.assertIsNone(res)
                mock_set.assert_not_called()

    def test_14_explorer_cf_hdrop_untouched(self):
        with patch("win32clipboard.IsClipboardFormatAvailable") as mock_avail:
            mock_avail.side_effect = lambda fmt: fmt == cp.win32con.CF_HDROP
            val = cp.get_clipboard_text()
            self.assertIsNone(val)

        with patch.object(cp, "get_clipboard_text", return_value=None):
            with patch.object(cp, "set_clipboard_text") as mock_set:
                res = cp.safe_copy_clipboard()
                self.assertIsNone(res)
                mock_set.assert_not_called()

    def test_15_ordinary_ctrl_c_never_automatically_sanitized(self):
        monitor = cp.LightweightClipboardMonitor.__new__(cp.LightweightClipboardMonitor)
        monitor.ignore_next_change = False
        monitor.last_clipboard_sequence = 1
        monitor.processed_hashes = set()
        monitor.save_path = "V:\\dummy"

        # When text is copied, get_clipboard_image returns (None, None)
        with patch.object(monitor, "get_clipboard_sequence", return_value=2):
            with patch.object(monitor, "get_clipboard_image", return_value=(None, None)):
                with patch.object(cp, "safe_copy_clipboard") as mock_safe_copy:
                    processed = monitor.process_clipboard()
                    self.assertFalse(processed)
                    self.assertEqual(monitor.last_clipboard_sequence, 2)
                    mock_safe_copy.assert_not_called()

    def test_16_image_monitor_behavior_preserved(self):
        monitor = cp.LightweightClipboardMonitor.__new__(cp.LightweightClipboardMonitor)
        monitor.ignore_next_change = False
        monitor.last_clipboard_sequence = 10
        monitor.processed_hashes = set()
        monitor.save_path = "V:\\dummy"

        fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRtest_data"
        with patch.object(monitor, "get_clipboard_sequence", return_value=11):
            with patch.object(monitor, "get_clipboard_image", return_value=(fake_png, "PNG")):
                with patch.object(monitor, "save_image_file", return_value=("V:\\dummy\\file.png", "file.png")):
                    with patch.object(monitor, "set_image_to_clipboard", return_value=True):
                        processed = monitor.process_clipboard()
                        self.assertTrue(processed)
                        fake_hash = cp.hashlib.md5(fake_png).hexdigest()
                        self.assertIn(fake_hash, monitor.processed_hashes)

    def test_17_no_test_output_contains_fixture_secrets(self):
        secret_fixture = "SUPER_SECRET_FIXTURE_KEY_xyz999"
        text = f"api_key = '{secret_fixture}'"
        captured_stdout = io.StringIO()
        with patch("sys.stdout", captured_stdout):
            res = cp.sanitize_share_text(text)
        output = captured_stdout.getvalue()
        self.assertNotIn(secret_fixture, output)
        self.assertNotIn(secret_fixture, res.text)

    def test_18_in_memory_restore_lifecycle(self):
        history = cp.SafeCopyHistory(ttl_seconds=1.0)
        history.store("original secret text 123")
        # First retrieve succeeds
        self.assertEqual(history.retrieve(), "original secret text 123")
        # Second retrieve is None (single use)
        self.assertIsNone(history.retrieve())

        # Test TTL expiration
        history.store("expiring text")
        time.sleep(1.1)
        self.assertIsNone(history.retrieve())


if __name__ == "__main__":
    unittest.main()
