"""Unit tests for Claude SAIPEN hook reconciliation (T-264 P0)."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from Scripts.consoles.claude_hooks import (
    inspect_claude_hooks,
    reconcile_claude_hooks,
    remove_claude_hooks,
    resolve_canonical_claude_hooks,
    CANONICAL_EVENTS,
)


class TestClaudeHooksReconciliation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="saituls_claude_hooks_test_"))
        self.claude1_dir = self.temp_dir / "claude1"
        self.claude2_dir = self.temp_dir / "claude2"
        self.claude1_dir.mkdir(parents=True)
        self.claude2_dir.mkdir(parents=True)

        # Mock canonical primary settings based on real Claude 1
        self.canonical_command = (
            "C:/py/python.exe -c 'import os,runpy,sys;a=sys.argv[1];sys.argv=sys.argv[1:];"
            "os.path.isfile(a) and runpy.run_path(a,run_name=__name__)' "
            "C:/Users/test/.claude/hooks/saipen-guard.py --host claude --saipen-root C:/Users/test/.claude/skills/saipen"
        )
        self.canonical_settings = {
            "model": "opus",
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Read",
                        "hooks": [{"type": "command", "command": "cbm-code-discovery.cmd", "timeout": 5}],
                    },
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": self.canonical_command, "timeout": 30}],
                    },
                ],
                "SessionStart": [
                    {
                        "hooks": [{"type": "command", "command": self.canonical_command, "timeout": 30}],
                    },
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [{"type": "command", "command": "printf '%s' style", "timeout": 5}],
                    },
                    {
                        "hooks": [{"type": "command", "command": self.canonical_command, "timeout": 30}],
                    },
                ],
                "PostModelSwitch": [
                    {
                        "hooks": [{"type": "command", "command": self.canonical_command, "timeout": 30}],
                    },
                ],
                "Stop": [
                    {
                        "hooks": [{"type": "command", "command": self.canonical_command, "timeout": 60}],
                    },
                ],
            },
            "statusLine": {"type": "command", "command": "statusline-bridge"},
            "modelSettings": {"claude-opus-5-5": {"effortLevel": "xhigh"}},
            "skipDangerousModePermissionPrompt": True,
            "theme": "auto",
        }
        (self.claude1_dir / "settings.json").write_text(
            json.dumps(self.canonical_settings, indent=2), encoding="utf-8"
        )
        self.catalog = resolve_canonical_claude_hooks(primary_dir=self.claude1_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_claude_1_already_correct_profile_remains_unchanged(self):
        """Claude 1 already-correct profile remains unchanged and byte-identical."""
        settings_path = self.claude1_dir / "settings.json"
        bytes_before = settings_path.read_bytes()

        insp = inspect_claude_hooks(self.claude1_dir, self.catalog)
        self.assertEqual(insp["status"], "CURRENT")
        self.assertEqual(insp["missing_events"], [])
        self.assertEqual(insp["drifted_events"], [])

        res = reconcile_claude_hooks(self.claude1_dir, self.catalog)
        self.assertEqual(res["status"], "ALREADY_CURRENT")
        self.assertFalse(res["mutated"])

        bytes_after = settings_path.read_bytes()
        self.assertEqual(bytes_before, bytes_after)

    def test_claude_2_missing_hook_fixture_is_repaired(self):
        """Claude 2 missing-hook fixture is repaired with canonical SAIPEN hooks."""
        claude2_initial = {
            "model": "opus",
            "modelSettings": {
                "claude-opus-5-5": {"effortLevel": "xhigh"},
                "claude-haiku-4-5": {"effortLevel": "low"},
            },
            "skipDangerousModePermissionPrompt": True,
            "theme": "auto",
        }
        (self.claude2_dir / "settings.json").write_text(
            json.dumps(claude2_initial, indent=2), encoding="utf-8"
        )

        insp_before = inspect_claude_hooks(self.claude2_dir, self.catalog)
        self.assertEqual(insp_before["status"], "MISSING_HOOKS")
        self.assertEqual(len(insp_before["missing_events"]), len(CANONICAL_EVENTS))

        res = reconcile_claude_hooks(self.claude2_dir, self.catalog)
        self.assertEqual(res["status"], "REPAIRED")
        self.assertTrue(res["mutated"])

        insp_after = inspect_claude_hooks(self.claude2_dir, self.catalog)
        self.assertEqual(insp_after["status"], "CURRENT")
        self.assertEqual(insp_after["missing_events"], [])
        self.assertEqual(insp_after["drifted_events"], [])

        # Verify hooks structure
        doc = json.loads((self.claude2_dir / "settings.json").read_text(encoding="utf-8"))
        hooks = doc["hooks"]
        for event, matcher, timeout in CANONICAL_EVENTS:
            self.assertIn(event, hooks)
            group = next(
                g for g in hooks[event]
                if (matcher is None and "matcher" not in g) or g.get("matcher") == matcher
            )
            owned = [h for h in group["hooks"] if "saipen-guard.py" in h.get("command", "")]
            self.assertEqual(len(owned), 1)
            self.assertEqual(owned[0]["command"], self.canonical_command)
            self.assertEqual(owned[0]["timeout"], timeout)

    def test_existing_unrelated_settings_survive_semantically_unchanged(self):
        """Existing unrelated settings survive intact."""
        custom_settings = {
            "model": "claude-haiku-4-5",
            "custom_token": "not_touched",
            "theme": "dark",
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Read",
                        "hooks": [{"type": "command", "command": "custom_read_hook.cmd", "timeout": 10}],
                    }
                ],
                "CustomEvent": [
                    {"hooks": [{"type": "command", "command": "something_else.exe"}]}
                ],
            },
        }
        (self.claude2_dir / "settings.json").write_text(
            json.dumps(custom_settings, indent=2), encoding="utf-8"
        )

        res = reconcile_claude_hooks(self.claude2_dir, self.catalog)
        self.assertTrue(res["mutated"])

        after = json.loads((self.claude2_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(after["model"], "claude-haiku-4-5")
        self.assertEqual(after["custom_token"], "not_touched")
        self.assertEqual(after["theme"], "dark")
        self.assertIn("CustomEvent", after["hooks"])
        # Custom Read hook survived
        post_tool = after["hooks"]["PostToolUse"]
        read_group = next(g for g in post_tool if g.get("matcher") == "Read")
        self.assertEqual(read_group["hooks"][0]["command"], "custom_read_hook.cmd")

    def test_malformed_configuration_fails_safely_without_corruption(self):
        """Malformed settings.json fails safely without mutating or truncating."""
        bad_json = '{"model": "opus", "hooks": [BROKEN'
        (self.claude2_dir / "settings.json").write_text(bad_json, encoding="utf-8")

        insp = inspect_claude_hooks(self.claude2_dir, self.catalog)
        self.assertEqual(insp["status"], "MALFORMED_SETTINGS")

        res = reconcile_claude_hooks(self.claude2_dir, self.catalog)
        self.assertEqual(res["status"], "MALFORMED_SETTINGS")
        self.assertFalse(res["mutated"])

        # File content was untouched
        self.assertEqual((self.claude2_dir / "settings.json").read_text(encoding="utf-8"), bad_json)

    def test_credentials_and_auth_files_never_read_or_copied(self):
        """Credentials, tokens, and session history in the profile directory are never read or touched."""
        # Create sensitive files in Claude 2
        (self.claude2_dir / ".credentials.json").write_text('{"token": "super_secret"}', encoding="utf-8")
        (self.claude2_dir / "sessions.json").write_text('{"session": "xyz"}', encoding="utf-8")
        (self.claude2_dir / "auth.json").write_text('{"key": "abc"}', encoding="utf-8")

        res = reconcile_claude_hooks(self.claude2_dir, self.catalog)
        self.assertTrue(res["mutated"])

        # Sensitive files exist untouched with identical content
        self.assertEqual((self.claude2_dir / ".credentials.json").read_text(encoding="utf-8"), '{"token": "super_secret"}')
        self.assertEqual((self.claude2_dir / "sessions.json").read_text(encoding="utf-8"), '{"session": "xyz"}')
        self.assertEqual((self.claude2_dir / "auth.json").read_text(encoding="utf-8"), '{"key": "abc"}')

    def test_reconciliation_run_twice_produces_no_second_mutation(self):
        """Reconciliation is strictly idempotent."""
        (self.claude2_dir / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")

        first = reconcile_claude_hooks(self.claude2_dir, self.catalog)
        self.assertTrue(first["mutated"])
        self.assertEqual(first["status"], "REPAIRED")

        content_after_first = (self.claude2_dir / "settings.json").read_bytes()

        second = reconcile_claude_hooks(self.claude2_dir, self.catalog)
        self.assertFalse(second["mutated"])
        self.assertEqual(second["status"], "ALREADY_CURRENT")

        content_after_second = (self.claude2_dir / "settings.json").read_bytes()
        self.assertEqual(content_after_first, content_after_second)

    def test_remove_claude_hooks_cleans_up_safely(self):
        """Removing hooks cleans up SAIPEN entries while keeping other settings."""
        reconcile_claude_hooks(self.claude2_dir, self.catalog)
        res = remove_claude_hooks(self.claude2_dir)
        self.assertTrue(res["mutated"])
        self.assertEqual(res["status"], "REMOVED")

        doc = json.loads((self.claude2_dir / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("hooks", doc)

    def test_remove_claude_hooks_skips_if_active_shared_profile(self):
        """Removing hooks refuses if another active account still uses the profile dir."""
        reconcile_claude_hooks(self.claude2_dir, self.catalog)
        res = remove_claude_hooks(self.claude2_dir, active_profile_dirs=[self.claude2_dir])
        self.assertFalse(res["mutated"])
        self.assertEqual(res["status"], "STILL_ACTIVE")


if __name__ == "__main__":
    unittest.main()
