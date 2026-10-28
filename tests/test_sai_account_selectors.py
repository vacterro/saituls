"""Account selection and explicit identity binding against an isolated plane.

Run after Scripts/accounts/build.ps1. The two test-only overrides also let
the unchanged oracle exercise a separately compiled pre-fix engine.
"""
import hashlib
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(os.environ.get("SAI_ACCOUNTS_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / "tests"))
import test_sai_accounts as fixture

if os.environ.get("SAI_ACCOUNTS_TEST_EXE"):
    fixture.EXE = os.environ["SAI_ACCOUNTS_TEST_EXE"]


class AccountContractTests(unittest.TestCase):
    def setUp(self):
        self.plane = fixture.Plane()
        self.addCleanup(self.plane.cleanup)
        # Import also discovers the operator's real profile directories. This
        # fixture owns its complete registry, including every selectable row.
        accounts = []
        for ordinal in (1, 2):
            sid = f"S-1-5-21-9-9-9-100{ordinal}"
            fingerprint = hashlib.sha256(sid.lower().encode()).hexdigest()[:16]
            accounts.append({
                "account_id": f"antigravity:windows-user:{fingerprint}",
                "provider_id": "antigravity", "provider_ordinal": ordinal,
                "display_name": None, "execution_backend": "windows_user",
                "execution_context_id": f"windows-user:{fingerprint}",
                "operational_state": "ENABLED", "hidden": False,
                "provider_metadata": {"windows_sid": sid, "windows_user": f"contract_{ordinal}"},
            })
        self.plane.write_registry({
            "schema": "sai.accounts/registry/2", "generation": 1,
            "operator_scope": {"scope_id": self.plane.scope, "operator_sid": self.plane.sid},
            "accounts": accounts,
        })
        self.first, self.second = [a["account_id"] for a in accounts]

    def aliases(self, first, second):
        reg = self.plane.registry()
        for row in reg["accounts"]:
            if row["account_id"] == self.first:
                row["display_name"] = first
            elif row["account_id"] == self.second:
                row["display_name"] = second
        self.plane.write_registry(reg)

    def persisted(self):
        scope = Path(self.plane.dir) / "scopes" / self.plane.scope
        return {name: (scope / name).read_bytes() if (scope / name).exists() else None
                for name in ("accounts.json", "events.log")}

    def ambiguity(self, *args, schema="sai.accounts/error/1"):
        before = self.persisted()
        code, result, _, _ = self.plane.run(*args)
        self.assertEqual(code, 1, result)
        self.assertEqual(result.get("status"), "ACCOUNT_SELECTOR_AMBIGUOUS", result)
        self.assertEqual(result.get("schema"), schema, result)
        self.assertEqual(set(result.get("candidates", [])), {self.first, self.second})
        self.assertIn("account_id", result.get("detail", ""))
        self.assertEqual(self.persisted(), before, "a refused selector changed persisted state")

    def test_compact_labels_and_ids_cannot_be_shadowed_by_aliases(self):
        self.aliases("AG2", self.first)
        for selector, target in (("ag2", self.second), (self.first, self.first), ("AG1", self.first)):
            with self.subTest(selector=selector):
                code, result, _, _ = self.plane.run("get", selector)
                self.assertEqual(code, 0, result)
                self.assertEqual(result["account"]["account_id"], target)
        self.plane.run("move", self.second, "before", self.first)
        code, result, _, _ = self.plane.run("get", "AG2")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["account"]["account_id"], self.second)

    def test_duplicate_aliases_refuse_reads(self):
        self.aliases("Shared account", "SHARED ACCOUNT")
        self.ambiguity("get", "shared account")
        self.ambiguity("status", "shared account")

    def test_duplicate_compact_labels_refuse_instead_of_picking_by_position(self):
        reg = self.plane.registry()
        for row in reg["accounts"]:
            row["provider_ordinal"] = 2
        self.plane.write_registry(reg)
        self.ambiguity("get", "AG2")
        code, result, _, _ = self.plane.run("get", self.second)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["account"]["account_id"], self.second)

    def test_duplicate_aliases_refuse_every_mutation_before_effects(self):
        self.aliases("Shared", "Shared")
        for args in (("rename", "Shared", "Changed"), ("set-hidden", "Shared", "true"),
                     ("set-state", "Shared", "DISABLED"), ("move", "Shared", "down"),
                     ("archive", "Shared"), ("logout", "Shared"),
                     ("context", "stop", "Shared"),
                     ("grant-workdir", "Shared", "--workdir", self.plane.dir)):
            with self.subTest(command=args[0]):
                self.ambiguity(*args)

    def test_ambiguous_launch_and_auth_keep_their_typed_envelopes(self):
        self.aliases("Shared", "Shared")
        self.ambiguity("launch", "Shared", "--workdir", self.plane.dir, schema="sai.accounts/launch/1")
        self.ambiguity("auth", "Shared", schema="sai.accounts/auth/1")

    def test_ambiguous_move_target_never_reorders(self):
        self.aliases("Shared", "Shared")
        self.ambiguity("move", self.second, "before", "Shared")

    def test_alias_collision_with_generated_display_name_refuses(self):
        self.aliases("Antigravity 2", None)
        self.ambiguity("get", "Antigravity 2")

    def test_legacy_and_alias_collision_refuses(self):
        self.aliases("Legacy", "Second")
        reg = self.plane.registry()
        for row in reg["accounts"]:
            if row["account_id"] == self.second:
                row["provider_metadata"]["legacy_id"] = "Legacy"
        self.plane.write_registry(reg)
        self.ambiguity("get", "Legacy")

    def test_duplicate_legacy_ids_refuse(self):
        reg = self.plane.registry()
        for row in reg["accounts"]:
            row["provider_metadata"]["legacy_id"] = "same-legacy"
        self.plane.write_registry(reg)
        self.ambiguity("get", "same-legacy")

    def test_unique_alias_still_resolves_and_id_stays_immutable(self):
        self.aliases("Work account", "Personal account")
        code, result, _, _ = self.plane.run("get", "WORK ACCOUNT")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["account"]["account_id"], self.first)

    def test_invalid_identity_fingerprints_never_persist(self):
        before = self.persisted()
        for fingerprint in ("", "short", "g" * 16, "f" * 32, "ya29.synthetic-secret", "1111\n222233334444"):
            with self.subTest(fingerprint=fingerprint):
                code, result, _, _ = self.plane.run("bind-identity", self.first,
                                                   "--fingerprint", fingerprint)
                self.assertEqual(code, 1, result)
                self.assertEqual(result.get("status"), "IDENTITY_INVALID", result)
                self.assertEqual(self.persisted(), before)

    def test_clear_cannot_silently_discard_binding_options(self):
        before = self.persisted()
        for option in (("--fingerprint", "AAAABBBBCCCCDDDD"), ("--label", "synthetic@example.com")):
            code, result, _, _ = self.plane.run("bind-identity", self.first, "--clear", *option)
            self.assertEqual(code, 1, result)
            self.assertEqual(result.get("status"), "bind_identity_args", result)
            self.assertEqual(self.persisted(), before)

    def test_valid_fingerprint_is_normalized_and_passive_reads_do_not_bind(self):
        code, result, _, _ = self.plane.run("bind-identity", self.first,
                                          "--fingerprint", "AAAABBBBCCCCDDDD",
                                          "--label", "synthetic@example.com")
        self.assertEqual(code, 0, result)
        before = self.persisted()
        code, result, _, _ = self.plane.run("get", self.first)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["account"]["configured_identity"]["fingerprint"], "aaaabbbbccccdddd")
        self.plane.run("list", "--all")
        self.assertEqual(self.persisted(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
