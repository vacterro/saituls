"""Agent console launcher regressions (Scripts/consoles).

Everything here is disposable: the launcher's ``-SelfTest`` mode resolves,
validates and reports, and starts no console. The one thing these tests
guard hardest is the elevation boundary -- the Explorer-menu OpenCode/Cline
launcher self-elevates to maximum privilege, and this subsystem deliberately
does not inherit that. An autonomous AI console that silently starts as
Administrator is a much bigger blast radius than the convenience is worth,
so "no RunAs anywhere" is asserted against the source, not just the output.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONSOLES = REPO / "Scripts" / "consoles"
LAUNCHER = CONSOLES / "CONSOLES.ps1"
REGISTRY = CONSOLES / "consoles.json"

LAUNCHER_SOURCE = LAUNCHER.read_text(encoding="utf-8")
REGISTRY_DOC = json.loads(REGISTRY.read_text(encoding="utf-8-sig"))
CLI_FIXTURES = tempfile.TemporaryDirectory(prefix="saituls-console-cli-")
unittest.addModuleCleanup(CLI_FIXTURES.cleanup)


def strip_powershell_comments(text):
    """Executable PowerShell only: <# #> blocks and # line comments removed.

    The launcher's own prose says what it deliberately does NOT do, so a
    substring search over the whole file would fail on its own explanation.
    """
    without_blocks = re.sub(r"<#.*?#>", "", text, flags=re.DOTALL)
    lines = []
    for line in without_blocks.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line.split("#", 1)[0] if "#" in line else line)
    return "\n".join(lines)


def strip_csharp_comments(text):
    """Executable C# only: /* */ blocks and // line comments removed."""
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    lines = []
    for line in without_blocks.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        lines.append(line.split("//", 1)[0] if "//" in line else line)
    return "\n".join(lines)


LAUNCHER_CODE = strip_powershell_comments(LAUNCHER_SOURCE)


def run_selftest(*extra, registry_path=None, env=None):
    cmd = ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(LAUNCHER), "-SelfTest"] + list(extra)
    merged = dict(os.environ)
    if env:
        merged.update(env)
    if registry_path:
        cmd += ["-RegistryPath", str(registry_path)]
    # SelfTest resolves commands without running them. Supply local fixtures
    # so this suite also runs on a clean Windows runner with no agent accounts.
    tmp = CLI_FIXTURES.name
    for name in ("claude", "agy", "codex", "cline", "opencode", "node"):
        Path(tmp, name + ".cmd").write_text("@echo off\nexit /b 0\n")
    script = Path(tmp, "zcode.cjs")
    script.write_text("// Resolution fixture; never executed.\n")
    merged["PATH"] = tmp + os.pathsep + merged.get("PATH", "")
    merged.setdefault("ZCODE_CLI", str(script))
    completed = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               cwd=str(REPO), env=merged, timeout=60)
    text = (completed.stdout or "").strip()
    start = text.find("{")
    payload = json.loads(text[start:]) if start >= 0 else None
    return completed.returncode, payload, completed.stderr


def profile_row(payload, profile_id):
    for row in payload["profiles"]:
        if row["Id"] == profile_id:
            return row
    raise AssertionError("profile %r absent from self-test output" % profile_id)


class RegistryTests(unittest.TestCase):
    def test_schema_is_pinned(self):
        self.assertEqual(REGISTRY_DOC["schema"], "saituls.consoles/1")
        self.assertEqual(REGISTRY_DOC["schema_version"], 1)

    def test_required_profiles_exist(self):
        ids = [p["id"] for p in REGISTRY_DOC["profiles"]]
        for wanted in ("claude-1", "claude-2", "antigravity"):
            self.assertIn(wanted, ids)

    def test_no_profile_carries_a_shell_command_string(self):
        for profile in REGISTRY_DOC["profiles"]:
            command = profile["command"]
            self.assertNotRegex(command, r"[\\/]",
                                "command must be a bare name, got %r" % command)
            self.assertNotIn(" ", command)
            self.assertIsInstance(profile["arguments"], list)
            for argument in profile["arguments"]:
                self.assertIsInstance(argument, str)

    def test_no_profile_requests_admin(self):
        for profile in REGISTRY_DOC["profiles"]:
            self.assertFalse(profile.get("requires_admin", False),
                             "profile %s must not request admin" % profile["id"])

    def test_no_profile_overrides_home(self):
        for profile in REGISTRY_DOC["profiles"]:
            keys = {k.upper() for k in (profile.get("environment") or {})}
            self.assertNotIn("HOME", keys)
            self.assertNotIn("USERPROFILE", keys)
            self.assertNotIn("PATH", keys)

    def test_claude_profiles_declare_distinct_config_dirs(self):
        by_id = {p["id"]: p for p in REGISTRY_DOC["profiles"]}
        one = by_id["claude-1"]["environment"]["CLAUDE_CONFIG_DIR"]
        two = by_id["claude-2"]["environment"]["CLAUDE_CONFIG_DIR"]
        self.assertNotEqual(one, two)
        self.assertTrue(one.endswith(".claude"))
        self.assertTrue(two.endswith(".claude-account2"))
        self.assertEqual(by_id["claude-1"]["command"],
                         by_id["claude-2"]["command"])


class SelfTestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rc, cls.payload, cls.stderr = run_selftest()

    def test_selftest_succeeds(self):
        self.assertEqual(self.rc, 0, self.stderr)
        self.assertIsNotNone(self.payload)
        self.assertEqual(self.payload["schema"], "saituls.consoles/1")

    def test_claude_1_environment(self):
        row = profile_row(self.payload, "claude-1")
        self.assertTrue(row["Resolved"], row["Error"])
        self.assertEqual(row["EnvironmentKeys"], ["CLAUDE_CONFIG_DIR"])
        expected = str(Path(os.environ["USERPROFILE"]) / ".claude")
        self.assertEqual(row["EnvironmentValues"]["CLAUDE_CONFIG_DIR"], expected)
        self.assertTrue(row["Command"].lower().endswith(
            ("claude.exe", "claude.cmd", "claude.bat", "claude")))

    def test_claude_2_environment(self):
        row = profile_row(self.payload, "claude-2")
        self.assertTrue(row["Resolved"], row["Error"])
        self.assertEqual(row["EnvironmentKeys"], ["CLAUDE_CONFIG_DIR"])
        expected = str(Path(os.environ["USERPROFILE"]) / ".claude-account2")
        self.assertEqual(row["EnvironmentValues"]["CLAUDE_CONFIG_DIR"], expected)

    def test_the_two_claude_profiles_resolve_the_same_command(self):
        one = profile_row(self.payload, "claude-1")
        two = profile_row(self.payload, "claude-2")
        self.assertEqual(one["Command"], two["Command"])
        self.assertNotEqual(one["EnvironmentValues"]["CLAUDE_CONFIG_DIR"],
                            two["EnvironmentValues"]["CLAUDE_CONFIG_DIR"])

    def test_antigravity_command_resolution(self):
        row = profile_row(self.payload, "antigravity")
        self.assertEqual(row["CommandName"], "agy")
        self.assertTrue(row["Resolved"], row["Error"])
        self.assertTrue(os.path.isfile(row["Command"]), row["Command"])
        self.assertEqual(row["EnvironmentKeys"], [])

    def test_console_launcher_remains_non_elevated(self):
        for row in self.payload["profiles"]:
            self.assertFalse(row["RequiresAdmin"],
                             "%s requests admin" % row["Id"])
            self.assertFalse(row["SelfElevates"],
                             "%s would self-elevate" % row["Id"])

    def test_project_first_title(self):
        with tempfile.TemporaryDirectory(prefix="saituls-console-") as tmp:
            project = Path(tmp) / "MyProject"
            project.mkdir()
            rc, payload, stderr = run_selftest("-Profile", "claude-1",
                                               "-WorkDir", str(project))
            self.assertEqual(rc, 0, stderr)
            row = profile_row(payload, "claude-1")
            self.assertEqual(row["WorkDir"], str(project))
            self.assertTrue(row["Title"].startswith("MyProject"), row["Title"])
            self.assertTrue(row["ProjectFirst"])
            self.assertIn("Claude 1", row["Title"])
            self.assertLessEqual(len(row["Title"]), 240)

    def test_title_is_truncated_below_the_osc_ceiling(self):
        with tempfile.TemporaryDirectory(prefix="saituls-console-") as tmp:
            deep = Path(tmp)
            for _ in range(12):
                deep = deep / ("segment-" + "x" * 18)
            deep.mkdir(parents=True)
            rc, payload, stderr = run_selftest("-Profile", "antigravity",
                                               "-WorkDir", str(deep))
            self.assertEqual(rc, 0, stderr)
            row = profile_row(payload, "antigravity")
            self.assertLessEqual(len(row["Title"]), 240)
            self.assertTrue(row["ProjectFirst"])

    def test_unknown_profile_is_reported_not_launched(self):
        rc, payload, _stderr = run_selftest("-Profile", "not-a-profile")
        self.assertEqual(rc, 0)
        self.assertFalse(payload["ok"])
        row = profile_row(payload, "not-a-profile")
        self.assertIn("unknown console profile", row["Error"])

    def test_missing_optional_script_preserves_the_other_profile_reports(self):
        rc, payload, stderr = run_selftest(env={"ZCODE_CLI": ""})
        self.assertEqual(rc, 0, stderr)
        self.assertFalse(payload["ok"])
        missing = profile_row(payload, "zcode")
        self.assertFalse(missing["Resolved"])
        self.assertIn("not installed", missing["Error"])
        self.assertEqual(missing["Arguments"], [])
        self.assertTrue(profile_row(payload, "claude-1")["Resolved"])


class EnvironmentIsolationTests(unittest.TestCase):
    def test_profile_environment_does_not_leak_into_the_parent_process(self):
        """The launcher must never mutate the environment that started it."""
        marker = "SAITULS-PARENT-SENTINEL"
        parent_env = {"CLAUDE_CONFIG_DIR": marker}
        rc, payload, stderr = run_selftest(env=parent_env)
        self.assertEqual(rc, 0, stderr)
        # The probe reports what the profile WOULD set...
        row = profile_row(payload, "claude-1")
        self.assertNotEqual(row["EnvironmentValues"]["CLAUDE_CONFIG_DIR"], marker)
        # ...and reports that it found the caller's value still in place,
        # i.e. it read the environment and did not overwrite it.
        self.assertIn("CLAUDE_CONFIG_DIR", row["EnvironmentLeaked"])
        # This python process is the parent of the parent; nothing touched it.
        self.assertNotEqual(os.environ.get("CLAUDE_CONFIG_DIR"), marker)

    def test_selftest_sets_no_environment_variable_at_all(self):
        before = dict(os.environ)
        run_selftest()
        self.assertEqual(dict(os.environ), before)

    def test_environment_is_applied_only_in_the_launch_path(self):
        # The only assignment in the script is inside the launch section,
        # after the self-test has already exited.
        assignments = [m.start() for m in
                       re.finditer(r"Set-Item -Path \(\"Env:\" \+", LAUNCHER_SOURCE)]
        self.assertEqual(len(assignments), 1)
        selftest_exit = LAUNCHER_SOURCE.index("$payload | ConvertTo-Json")
        self.assertGreater(assignments[0], selftest_exit)


class ElevationBoundaryTests(unittest.TestCase):
    def test_no_self_elevation_anywhere_in_the_launcher(self):
        """No RunAs in executable code. Prose explaining its absence is fine."""
        self.assertNotIn("RunAs", LAUNCHER_CODE)
        self.assertNotIn("runas", LAUNCHER_CODE.lower().replace("runasadmin", ""))
        # The stripper must not have eaten the file: sanity-check that real
        # code survived, or this assertion would pass on an empty string.
        self.assertIn("Resolve-ConsoleCommand", LAUNCHER_CODE)
        self.assertIn("Start-Process", LAUNCHER_CODE)
        # ...and that the prose the stripper removed really does exist.
        self.assertIn("RunAs", LAUNCHER_SOURCE)

    def test_admin_is_checked_only_to_refuse_not_to_elevate(self):
        self.assertIn("requires_admin", LAUNCHER_SOURCE)
        self.assertIn("the launcher does not elevate itself", LAUNCHER_SOURCE)

    def test_new_console_is_spawned_without_elevation(self):
        block = re.search(r"Start-Process -FilePath 'powershell\.exe'.*?\)",
                          LAUNCHER_SOURCE, re.DOTALL)
        self.assertIsNotNone(block)
        self.assertNotIn("Verb", block.group(0))

    def test_legacy_agent_launcher_is_untouched(self):
        """The existing OpenCode/Cline path keeps its own behaviour."""
        legacy = (REPO / "Scripts" / "AI_AGENT_LAUNCHER.PS1").read_text(encoding="utf-8")
        self.assertIn("-Verb RunAs", legacy)
        self.assertIn("Maximum-privilege elevation", legacy)


class ConsoleBehaviourTests(unittest.TestCase):
    def test_command_is_resolved_before_launch(self):
        resolve_at = LAUNCHER_SOURCE.index("$command = Resolve-ConsoleCommand $consoleProfile")
        invoke_at = LAUNCHER_SOURCE.index("& $command @arguments")
        self.assertLess(resolve_at, invoke_at)

    def test_missing_command_produces_a_dependency_error(self):
        self.assertIn("was not found on PATH", LAUNCHER_SOURCE)
        self.assertIn("Install the CLI for", LAUNCHER_SOURCE)

    def test_window_stays_open_on_a_nonzero_exit(self):
        self.assertIn("if ($exitCode -ne 0)", LAUNCHER_SOURCE)
        self.assertIn("Press Enter to close this window", LAUNCHER_SOURCE)
        self.assertIn("Read-Host", LAUNCHER_SOURCE)

    def test_ctrl_c_and_ctrl_v_are_preserved(self):
        self.assertIn("ENABLE_PROCESSED_INPUT", LAUNCHER_SOURCE)
        self.assertIn("ENABLE_QUICK_EDIT_MODE", LAUNCHER_SOURCE)
        self.assertIn("ENABLE_INSERT_MODE", LAUNCHER_SOURCE)
        self.assertIn("ENABLE_EXTENDED_FLAGS", LAUNCHER_SOURCE)
        # No Ctrl+C handler of the launcher's own: the agent owns the signal.
        self.assertNotIn("SetConsoleCtrlHandler", LAUNCHER_SOURCE)
        self.assertNotIn("TreatControlCAsInput", LAUNCHER_SOURCE)

    def test_working_directory_is_asked_for(self):
        self.assertIn("FolderBrowserDialog", LAUNCHER_SOURCE)
        for profile in REGISTRY_DOC["profiles"]:
            self.assertEqual(profile["working_directory_mode"], "ask")

    def test_title_guard_matches_the_project_first_contract(self):
        self.assertIn("SaitulsConsole.TitleGuard", LAUNCHER_SOURCE)
        self.assertIn("SetConsoleTitleW", LAUNCHER_SOURCE)

    def test_forbidden_environment_keys_are_refused_by_validation(self):
        self.assertIn("$FORBIDDEN_ENV", LAUNCHER_SOURCE)
        for key in ("HOME", "USERPROFILE", "PATH"):
            self.assertIn("'%s'" % key, LAUNCHER_SOURCE)

    def test_bad_registry_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="saituls-console-bad-") as tmp:
            bad = Path(tmp) / "consoles.json"
            bad.write_text(json.dumps({"schema": "saituls.consoles/9",
                                       "schema_version": 9, "profiles": []}),
                           encoding="utf-8")
            probe = Path(tmp) / "CONSOLES.ps1"
            probe.write_text(LAUNCHER_SOURCE, encoding="utf-8")
            completed = subprocess.run(
                ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(probe), "-SelfTest"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unsupported console registry schema",
                          (completed.stderr or "") + (completed.stdout or ""))


class SaitulsIntegrationTests(unittest.TestCase):
    """The shell exposes the subsystems and keeps owning none of their logic."""

    SOURCE = (REPO / "SAITULS.cs").read_text(encoding="utf-8")
    CODE = strip_csharp_comments(SOURCE)

    def test_tools_tab_exposes_both_subsystems(self):
        self.assertIn('{ "Secure Apps", "secureapps",        "secureapps" }', self.SOURCE)
        self.assertIn('{ "AI Consoles", "consoles",          "consoles" }', self.SOURCE)

    def test_shell_launches_the_subsystem_scripts(self):
        self.assertIn('"Scripts", "secure_apps", "SECURE_APPS.ps1"', self.SOURCE)
        self.assertIn('"Scripts", "consoles", "CONSOLES.ps1"', self.SOURCE)

    def test_shell_never_elevates_the_subsystems(self):
        block = re.search(r'if \(mode == "secureapps" \|\| mode == "consoles".*?\).*?return;\n            \}',
                          self.CODE, re.DOTALL)
        self.assertIsNotNone(block)
        self.assertNotIn("runas", block.group(0).lower())
        self.assertIn("UseShellExecute = false", block.group(0))

    def test_shell_holds_no_security_logic(self):
        """Comments may name the boundary; executable code may not cross it."""
        lowered = self.CODE.lower()
        # Sanity: the stripper left real code behind, so assertNotIn below
        # is testing something rather than passing on an empty string.
        self.assertIn("secureappsstatusline", lowered)
        for forbidden in ("fido2", "hmac-secret", "bitlocker", "aesgcm",
                          "unlock-bitlocker", "mount-diskimage", "hkdf",
                          "diskpart", "credential"):
            self.assertNotIn(forbidden, lowered,
                             "SAITULS.cs code must not contain %r" % forbidden)

    def test_shell_status_reads_only_the_non_secret_state_file(self):
        self.assertIn("SecureAppsStatusLine", self.CODE)
        self.assertIn('"state", "secure-apps.json"', self.CODE)
        self.assertNotIn("credentials.json", self.SOURCE)
        self.assertNotIn("audit.log", self.SOURCE)

    def test_existing_tool_entries_are_unchanged(self):
        for existing in ('{ "OpenCode Patcher", "saipatch",     "saipatch" }',
                         '{ "OpenCode Settings", "saipatch",    "saipatch-settings" }',
                         '{ "OpenCode Queue Viewer", "saipatch", "saipatch-queue-viewer" }',
                         '{ "Scenarios", "scenarios",           "scenarios" }',
                         '{ "Del Junk",     "DEL_JUNK.PYW",     "folder" }'):
            self.assertIn(existing, self.SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
