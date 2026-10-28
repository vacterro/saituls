import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Scripts" / "secure_apps"))

import sa_crypto
import sa_mnemonic


class MnemonicTests(unittest.TestCase):
    def test_bitlocker_key_roundtrip(self):
        # 8 valid groups divisible by 11
        key = "459558-713317-522874-502887-319836-485991-377014-270061"
        phrase = sa_mnemonic.bitlocker_to_mnemonic(key)
        self.assertIsNotNone(phrase)
        words = phrase.split()
        self.assertEqual(len(words), 12)
        recovered_key = sa_mnemonic.mnemonic_to_bitlocker(phrase)
        self.assertEqual(key, recovered_key)

    def test_invalid_key_rejected(self):
        # Not divisible by 11
        bad_key = "123456-123456-123456-123456-123456-123456-123456-123456"
        self.assertIsNone(sa_mnemonic.bitlocker_to_mnemonic(bad_key))

        # Too short
        self.assertIsNone(sa_mnemonic.bitlocker_to_mnemonic("111111-222222"))

    def test_invalid_phrase_rejected(self):
        # Unknown word
        bad_phrase = "abandon ability able xyznotaword test foo bar baz qux quux corge grault"
        self.assertIsNone(sa_mnemonic.mnemonic_to_bitlocker(bad_phrase))

        # Wrong word count
        self.assertIsNone(sa_mnemonic.mnemonic_to_bitlocker("abandon ability able"))

    def test_portable_passphrase_generation(self):
        phrase = sa_mnemonic.generate_portable_passphrase(word_count=9)
        words = phrase.split()
        self.assertEqual(len(words), 9)
        for w in words:
            self.assertIn(w, sa_mnemonic.BIP39_WORDS)

    def test_master_password_validation(self):
        # Good passphrase
        ok, msg = sa_mnemonic.validate_master_password("correct horse battery staple blue sky winter snow")
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        # Generated 9-word phrase
        gen = sa_mnemonic.generate_portable_passphrase(9)
        ok, msg = sa_mnemonic.validate_master_password(gen)
        self.assertTrue(ok)

        # Too short (single password < 16 chars)
        ok, msg = sa_mnemonic.validate_master_password("short123!")
        self.assertFalse(ok)
        self.assertIn("at least 16 characters", msg)

        # Too few words (< 8-9 words)
        ok, msg = sa_mnemonic.validate_master_password("apple banana cherry date")
        self.assertFalse(ok)
        self.assertIn("at least 8-9 words", msg)

        # Repeated single words
        ok, msg = sa_mnemonic.validate_master_password("apple apple apple apple apple apple apple")
        self.assertFalse(ok)
        self.assertIn("repeated single words", msg)

        # Common bad passwords
        ok, msg = sa_mnemonic.validate_master_password("password12345678")
        self.assertFalse(ok)
        self.assertIn("too common", msg)

    def test_word_challenge_generation(self):
        phrase = "alpha beta gamma delta epsilon zeta eta theta iota"
        challenge = sa_mnemonic.generate_word_challenge(phrase, challenge_count=3)
        self.assertEqual(len(challenge), 3)
        words = phrase.split()
        for idx, word in challenge:
            self.assertTrue(1 <= idx <= len(words))
            self.assertEqual(words[idx - 1], word)


if __name__ == "__main__":
    unittest.main()
