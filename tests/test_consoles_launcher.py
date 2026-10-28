"""Agent console launcher regressions (Scripts/consoles).

Everything here is disposable: the launcher's ``-SelfTest`` mode resolves,
validates and reports, and starts no console. The one thing these tests
guard hardest is the elevation boundary -- the Explorer-menu OpenCode/Cline
launcher self-elevates to maximum privilege, and this subsystem deliberately
does not inherit that. An autonomous AI console that silently starts as
Administrator is a much bigger blast radius than the convenience is worth,
so "no RunAs anywhere" is asserted against the source, not just the output.
"""
import atexit
import json
import os
import re
import shutil
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


def run_selftest(*extra, registry_path=None, env=None, federation="off"):
    """Every suite here runs STANDALONE unless it says otherwise.

    SAI Accounts is optional, so the local profile set must be identical on a
    machine that has the control plane installed and one that does not. Pinning
    -Federation off makes that a fact the suite checks rather than a thing that
    depends on which machine happens to run it.
    """
    cmd = ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(LAUNCHER), "-SelfTest", "-Federation", federation] + list(extra)
    merged = dict(os.environ)
    if env:
        merged.update(env)
    completed = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               cwd=str(REPO), env=merged)
    text = (completed.stdout or "").strip()
    start = text.find("{")
    payload = json.loads(text[start:]) if start >= 0 else None
    return completed.returncode, payload, completed.stderr

def is_template(profile):
    """A profile that renders one console per canonical account, not a console."""
    return bool(profile.get("expand_accounts"))


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
            if is_template(profile):
                continue
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
            (Path(tmp) / "ACCOUNTS.ps1").write_text(ACCOUNTS_SOURCE, encoding="utf-8")
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

    def assert_tool_entry(self, key, label, command, mode):
        # Tool rows now hold localization keys. Verify both the stable launch
        # binding and its English label without coupling to spacing or locale.
        self.assertRegex(self.CODE, r'\{\s*"%s"\s*,\s*"%s"\s*,\s*"%s"\s*\}' %
                         tuple(re.escape(value) for value in (key, command, mode)))
        self.assertRegex(self.CODE, r'\{\s*"%s"\s*,\s*"%s"\s*\}' %
                         (re.escape(key), re.escape(label)))

    def test_tools_tab_exposes_both_subsystems(self):
        self.assert_tool_entry("ToolSecureApps", "Secure Apps", "secureapps", "secureapps")
        self.assert_tool_entry("ToolAiConsoles", "AI Consoles", "consoles", "consoles")

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
        """The shell reads sanitized account facts; it owns no secret or primitive.

        The boundary is not the word ``credential``. The Accounts control plane
        publishes structured, already-sanitized fields -- ``credential_present``,
        ``credential.provider_identity.fingerprint``, ``account_label``,
        ``auth_state`` -- and the account tab is supposed to read exactly those,
        through the canonical CLI. What is forbidden is the shell reaching a
        credential ITSELF: the vendor's credential file, a raw provider token, the
        credential store, FIDO2, BitLocker, a KDF or an encryption primitive. A
        bare-word grep cannot tell those apart, so this test separates them: the
        primitive and raw-material list below is the prohibition, and the named
        fields are the allowlist the shell may consume.
        """
        lowered = self.CODE.lower()
        # Sanity: the stripper left real code behind, so the assertions below
        # are testing something rather than passing on an empty string.
        self.assertIn("secureappsstatusline", lowered)
        # Security primitives and raw credential material: owned by Secure Apps
        # and by the Accounts engine, never by this shell.
        for forbidden in ("fido2", "hmac-secret", "webauthn", "ctap2",
                          "bitlocker", "unlock-bitlocker",
                          "add-bitlockerkeyprotector", "manage-bde",
                          "mount-diskimage", "dismount-diskimage", "diskpart",
                          "hkdf", "aesgcm", "pbkdf2", "rfc2898",
                          "cryptprotectdata", "cryptunprotectdata",
                          "system.security.cryptography", "credentials.json",
                          "auth.json", "access_token", "refresh_token",
                          "id_token", "client_secret", "cmdkey",
                          "credentialmanager", "credwrite", "creddelete"):
            self.assertNotIn(forbidden, lowered,
                             "SAITULS.cs code must not contain %r" % forbidden)
        # Every credential-named token in executable code must be one of the
        # sanitized control-plane fields, plus the helper that waits for the
        # engine to report one gone, plus the engine admission verdict. A name
        # that is not on this list means the shell grew an opinion about a
        # credential instead of reading its state.
        sanitized = {"credential", "credential_present", "waitforcredentialgone",
                     "credentials_unavailable"}
        # A display-only help message names "credentials" in ordinary prose.
        # Exclude exactly that known literal from the identifier check; the raw
        # material and primitive prohibitions above still inspect every string.
        help_literal = '"Uses your Claude Code environment and credentials.\\nEach account gets its own config home."'.lower()
        self.assertEqual(lowered.count(help_literal), 1)
        named = {word for word in re.findall(r"[a-z_][a-z0-9_]*", lowered.replace(help_literal, '""'))
                 if "credential" in word}
        self.assertTrue(named,
                        "the sanitized credential fields are this boundary's subject")
        self.assertEqual(sorted(named - sanitized), [],
                         "SAITULS.cs may name only sanitized credential fields")
        # ...and they arrive through the account plane's own CLI, not a file of
        # ours: the registry is the engine's, and only the engine reads it.
        self.assertIn("sai-accounts", lowered)
        self.assertNotIn("accounts.json", lowered)

    def test_shell_status_reads_only_the_non_secret_state_file(self):
        self.assertIn("SecureAppsStatusLine", self.CODE)
        self.assertIn('"state", "secure-apps.json"', self.CODE)
        self.assertNotIn("credentials.json", self.SOURCE)
        self.assertNotIn("audit.log", self.SOURCE)

    def test_existing_tool_entries_are_unchanged(self):
        for entry in (("ToolPatcher", "OpenCode Patcher", "saipatch", "saipatch"),
                      ("ToolOpenCodeSettings", "OpenCode Settings", "saipatch", "saipatch-settings"),
                      ("ToolQueueViewer", "OpenCode Queue Viewer", "saipatch", "saipatch-queue-viewer"),
                      ("ToolScenarios", "Scenarios", "scenarios", "scenarios"),
                      ("ToolDelJunk", "Del Junk", "DEL_JUNK.PYW", "folder")):
            self.assert_tool_entry(*entry)


ACCOUNTS_MODULE = CONSOLES / "ACCOUNTS.ps1"
ACCOUNTS_SOURCE = ACCOUNTS_MODULE.read_text(encoding="utf-8")
ACCOUNTS_CODE = strip_powershell_comments(ACCOUNTS_SOURCE)
INSTALLER = REPO / "Installers" / "INSTALL_CONSOLES_MENU.PS1"
INSTALLER_SOURCE = INSTALLER.read_text(encoding="utf-8")

def ps(script, env=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    completed = subprocess.run(
        ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                    + ". '%s'; %s" % (ACCOUNTS_MODULE, script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), env=merged)
    return completed.returncode, (completed.stdout or "").strip(), completed.stderr or ""

def engine_is_installed():
    rc, out, _err = ps("$e = Resolve-SaiAccountsEngine; if ($e) { 'yes' } else { 'no' }")
    return rc == 0 and out == "yes"

class StandaloneTests(unittest.TestCase):
    """A machine with no control plane is the ordinary case, not a degraded one."""

    def test_standalone_reports_no_engine_and_no_accounts(self):
        rc, payload, stderr = run_selftest(federation="off")
        self.assertEqual(rc, 0, stderr)
        self.assertIsNone(payload["federation"]["engine"])
        self.assertEqual(payload["federation"]["accounts_seen"], 0)
        self.assertEqual(payload["federation"]["shared_profiles"], 0)

    def test_standalone_lists_exactly_the_local_profiles(self):
        rc, payload, stderr = run_selftest(federation="off")
        self.assertEqual(rc, 0, stderr)
        local = [p["Id"] for p in payload["profiles"] if p["Origin"] == "local"]
        declared = [p["id"] for p in REGISTRY_DOC["profiles"] if not is_template(p)]
        self.assertEqual(local, declared)
        self.assertTrue(all(p["AccountId"] is None for p in payload["profiles"]))

    def test_standalone_keeps_every_local_profile_launchable(self):
        rc, payload, stderr = run_selftest(federation="off")
        self.assertEqual(rc, 0, stderr)
        self.assertTrue(payload["ok"], payload["profiles"])
        # The tools that ship here (zcode, cline, opencode) are not accounts and
        # must survive federation untouched: they own no identity to reconcile.
        for tool in ("zcode", "cline", "opencode"):
            row = profile_row(payload, tool)
            self.assertEqual(row["Origin"], "local")
            self.assertEqual(row["CommandName"],
                             [p["command"] for p in REGISTRY_DOC["profiles"]
                              if p["id"] == tool][0])

    def test_absent_engine_yields_no_accounts_and_no_exception(self):
        rc, out, err = ps("@(Get-SaiAccountDescriptors $null).Count")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "0")
        rc, out, err = ps("@(Get-SaiAccountDescriptors 'C:\\nope\\sai-accounts.exe').Count")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "0")

class AccountProfileShapeTests(unittest.TestCase):
    """The adapter's launch shape, proven on every machine.

    ConvertTo-AccountProfile is the ONE place where a canonical account becomes
    a console profile, and it is shared by every provider, so pinning it here
    covers Claude, Codex and Antigravity at once and keeps running when the
    control plane is not installed.

    The flag is not a style choice. SaiAccounts.cs parses a workdir only after
    --workdir; a positional directory is ignored, the engine is handed
    workdir=null, and every Explorer "Open in" launch dies with
    WORKDIR_UNAVAILABLE / NOT_FOUND. That is the live failure this suite exists
    to keep fixed.
    """

    def generated(self, provider, compact, account_id):
        account = ("[pscustomobject]@{ account_id='%s'; "
                   "provider_id='%s'; display_name='%s'; compact='%s'; "
                   "state='ENABLED'; hidden=$false; launchable=$true; "
                   "locator='C:\\profiles\\p'; identity='%s|c:\\profiles\\p' }"
                   % (account_id, provider, compact, compact, provider))
        template = ("[pscustomobject]@{ id='shared-%s'; label='%s'; "
                    "expand_accounts=$true; provider='%s' }" % (provider, provider, provider))
        rc, out, err = ps("$t=%s; $a=%s; ConvertTo-AccountProfile $t $a 'sai-accounts.exe' "
                         "| ConvertTo-Json -Depth 5" % (template, account))
        self.assertEqual(rc, 0, err)
        return json.loads(out[out.find("{"):])

    def assert_launch_shape(self, profile, account_id):
        self.assertEqual(profile["arguments"],
                         ["launch", account_id, "--workdir", "{workdir}"])

    def test_claude_profile_uses_the_workdir_flag(self):
        account_id = "claude:profile-dir:0123456789abcdef"
        self.assert_launch_shape(self.generated("claude", "CL2", account_id), account_id)

    def test_codex_profile_uses_the_workdir_flag(self):
        account_id = "codex:profile-dir:0123456789abcdef"
        self.assert_launch_shape(self.generated("codex", "CX2", account_id), account_id)

    def test_antigravity_profile_uses_the_workdir_flag(self):
        account_id = "antigravity:windows-user:0123456789abcdef"
        self.assert_launch_shape(self.generated("antigravity", "AG2", account_id), account_id)

    def test_every_shipped_template_provider_uses_the_same_shape(self):
        """A provider added later inherits the contract instead of a new bug."""
        templates = [p for p in REGISTRY_DOC["profiles"] if is_template(p)]
        self.assertTrue(templates)
        for template in templates:
            provider = template["provider"]
            with self.subTest(provider=provider):
                account_id = "%s:profile-dir:0123456789abcdef" % provider
                self.assert_launch_shape(
                    self.generated(provider, "Z9", account_id), account_id)

    def test_the_adapter_source_carries_no_positional_workdir(self):
        """Guard the text the contract is written in, not just the output."""
        body = re.search(r"function ConvertTo-AccountProfile.*?\n}", ACCOUNTS_CODE,
                         re.DOTALL).group(0)
        arguments = re.search(r"arguments\s*=.*", body).group(0)
        self.assertIn("'--workdir'", arguments)
        self.assertIn("'{workdir}'", arguments)
        self.assertNotRegex(arguments, r"'launch',\s*\[[^\]]+\],\s*'\{workdir\}'")

    def test_the_account_engine_parses_the_workdir_only_after_the_flag(self):
        """The caller is repaired to the engine, not the engine to the caller."""
        engine = (REPO / "Scripts" / "accounts" / "SaiAccounts.cs").read_text(encoding="utf-8")
        launch = re.search(r'if \(action == "launch"\).*?if \(action == "grant-workdir"\)',
                           engine, re.DOTALL).group(0)
        self.assertIn('args[i] == "--workdir"', launch)
        self.assertNotIn("args[2]", launch.split("LaunchAccount")[0])


class FederationTests(unittest.TestCase):
    """The control plane adds accounts. It never takes any away."""

    def setUp(self):
        if not engine_is_installed():
            self.skipTest("SAI Accounts control plane is not installed on this machine")

    def federated(self):
        rc, payload, stderr = run_selftest(federation="on")
        self.assertEqual(rc, 0, stderr)
        self.assertTrue(payload["ok"], payload["profiles"])
        return payload

    def test_federated_adds_shared_accounts(self):
        payload = self.federated()
        shared = [p for p in payload["profiles"] if p["Origin"] == "shared"]
        self.assertTrue(shared)
        self.assertEqual(payload["federation"]["shared_profiles"], len(shared))
        self.assertTrue(payload["federation"]["engine"])

    def test_shared_profiles_launch_by_canonical_account_id(self):
        payload = self.federated()
        for row in payload["profiles"]:
            if row["Origin"] != "shared":
                continue
            # launch, the canonical id, --workdir and the working directory -
            # nothing else. The flag is the contract: SaiAccounts.cs parses a
            # workdir only after --workdir, so a positional directory is dropped
            # silently and the engine launches with a null workdir.
            self.assertEqual(row["Arguments"][0], "launch")
            self.assertEqual(row["Arguments"][1], row["AccountId"])
            self.assertEqual(row["Arguments"][2], "--workdir")
            self.assertEqual(len(row["Arguments"]), 4)
            self.assertEqual(row["Arguments"][3], row["WorkDir"])
            self.assertRegex(row["AccountId"],
                             r"^(antigravity|claude|codex):(windows-user|profile-dir):[0-9a-f]{16}$")
            # The control plane owns the environment; a shared profile must not
            # smuggle one in beside it.
            self.assertEqual(row["EnvironmentKeys"], [])

    def test_shared_profiles_resolve_to_the_engine_not_to_path(self):
        payload = self.federated()
        for row in payload["profiles"]:
            if row["Origin"] != "shared":
                continue
            self.assertTrue(row["Resolved"], row["Error"])
            self.assertTrue(row["Command"].lower().endswith("sai-accounts.exe"))
            self.assertFalse(row["RequiresAdmin"])
            self.assertFalse(row["SelfElevates"])

    def shared_rows(self, payload, provider):
        """Shared rows of one provider, matched on the canonical id, not a label."""
        return [r for r in payload["profiles"]
                if r["Origin"] == "shared" and r["AccountId"].startswith(provider + ":")]

    def assert_four_part_shape(self, rows):
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["Arguments"][0], "launch")
            self.assertEqual(row["Arguments"][1], row["AccountId"])
            self.assertEqual(row["Arguments"][2], "--workdir")
            self.assertEqual(len(row["Arguments"]), 4)
            self.assertEqual(row["Arguments"][3], row["WorkDir"])

    def test_antigravity_shared_profiles_use_the_four_part_shape(self):
        payload = self.federated()
        self.assert_four_part_shape(self.shared_rows(payload, "antigravity"))

    def test_claude_shared_profiles_use_the_four_part_shape(self):
        payload = self.federated()
        self.assert_four_part_shape(self.shared_rows(payload, "claude"))

    def test_codex_shared_profiles_use_the_four_part_shape(self):
        payload = self.federated()
        self.assert_four_part_shape(self.shared_rows(payload, "codex"))

    def test_a_workdir_with_spaces_stays_exactly_one_argument(self):
        with tempfile.TemporaryDirectory(prefix="saituls-console-") as tmp:
            project = Path(tmp) / "My Big Project 2"
            project.mkdir()
            rc, payload, stderr = run_selftest("-WorkDir", str(project), federation="on")
            self.assertEqual(rc, 0, stderr)
            shared = [p for p in payload["profiles"] if p["Origin"] == "shared"]
            self.assertTrue(shared)
            for row in shared:
                self.assertEqual(row["WorkDir"], str(project))
                self.assertEqual(row["Arguments"],
                                 ["launch", row["AccountId"], "--workdir", str(project)])

    def test_a_unicode_workdir_survives_intact(self):
        with tempfile.TemporaryDirectory(prefix="saituls-console-") as tmp:
            project = Path(tmp) / "проект Ünïcødé"
            project.mkdir()
            rc, payload, stderr = run_selftest("-WorkDir", str(project), federation="on")
            self.assertEqual(rc, 0, stderr)
            shared = [p for p in payload["profiles"] if p["Origin"] == "shared"]
            self.assertTrue(shared)
            for row in shared:
                self.assertEqual(row["Arguments"][3], str(project))
                self.assertIn("проект Ünïcødé", row["Arguments"][3])

    def test_a_drive_root_workdir_survives_resolution(self):
        """Explorer hands over the root as "%V."; the engine must still get "X:\\".

        Only the quote-escaped root is exercised here: the trailing-dot form is
        expanded by the shell against the folder the Explorer process already
        runs in, so asserting it would test GetFullPath, not this launcher.
        """
        root = (os.environ.get("SystemDrive") or "C:") + "\\"
        rc, payload, stderr = run_selftest("-WorkDir", root, federation="on")
        self.assertEqual(rc, 0, stderr)
        shared = [p for p in payload["profiles"] if p["Origin"] == "shared"]
        self.assertTrue(shared)
        for row in shared:
            self.assertEqual(row["WorkDir"], root)
            self.assertEqual(row["Arguments"][3], root)

    def test_no_shared_launch_reaches_the_engine_without_a_workdir(self):
        """The live failure was workdir=null; a null here is the same failure."""
        with tempfile.TemporaryDirectory(prefix="saituls-console-") as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            rc, payload, stderr = run_selftest("-WorkDir", str(project), federation="on")
            self.assertEqual(rc, 0, stderr)
            shared = [p for p in payload["profiles"] if p["Origin"] == "shared"]
            self.assertTrue(shared)
            for row in shared:
                workdir = row["Arguments"][3]
                self.assertTrue(workdir)
                self.assertNotEqual(workdir.lower(), "null")
                self.assertNotIn("null", [a.lower() for a in row["Arguments"]])
                self.assertTrue(os.path.isdir(workdir))

    def test_federation_changes_nothing_for_a_local_profile(self):
        rc, standalone, stderr = run_selftest(federation="off")
        self.assertEqual(rc, 0, stderr)
        federated = self.federated()
        local = {p["Id"]: p for p in federated["profiles"] if p["Origin"] == "local"}
        self.assertTrue(local)
        for row in standalone["profiles"]:
            if row["Id"] not in local:
                continue
            self.assertEqual(local[row["Id"]]["Arguments"], row["Arguments"])
            self.assertEqual(local[row["Id"]]["Command"], row["Command"])

    def test_antigravity_two_keeps_its_own_account_id(self):
        """Identity is the canonical id: AG2 must never resolve to AG1."""
        payload = self.federated()
        rc, ag2_id, err = ps("$a = Get-SaiAccountDescriptors (Resolve-SaiAccountsEngine) | "
                             "Where-Object { $_.compact -eq 'AG2' }; $a.account_id")
        self.assertEqual(rc, 0, err)
        ag2_id = ag2_id.strip()
        self.assertRegex(ag2_id, r"^antigravity:windows-user:[0-9a-f]{16}$")
        ids = [r["AccountId"] for r in payload["profiles"]
               if r["Origin"] == "shared" and r["AccountId"].startswith("antigravity:")]
        self.assertIn(ag2_id, ids)
        ag1 = [i for i in ids if i != ag2_id]
        for other in ag1:
            self.assertNotEqual(other, ag2_id)
        for row in payload["profiles"]:
            if row["Origin"] == "shared" and row["AccountId"] in ag1:
                self.assertNotEqual(row["Arguments"][1], ag2_id)

    def test_no_account_appears_twice(self):
        payload = self.federated()
        ids = [p["Id"] for p in payload["profiles"]]
        self.assertEqual(len(ids), len(set(ids)))
        account_ids = [p["AccountId"] for p in payload["profiles"] if p["AccountId"]]
        self.assertEqual(len(account_ids), len(set(account_ids)))
        labels = [p["Label"] for p in payload["profiles"]]
        # A canonical account that duplicates a local profile is collapsed, so
        # the launcher must never list the same physical thing twice.
        self.assertEqual(len(labels), len(set(labels)))

    def test_local_and_canonical_views_of_one_account_collapse(self):
        """Same profile directory on both sides -> one row, origin shared."""
        home = os.environ["USERPROFILE"]
        local = [p for p in REGISTRY_DOC["profiles"] if not is_template(p)]
        collapsed = 0
        for profile in local:
            if profile.get("provider") != "claude":
                continue
            locator = os.path.normcase(os.path.normpath(
                profile["environment"]["CLAUDE_CONFIG_DIR"].replace("%USERPROFILE%", home)))
            out = ps("$d = Get-Content '%s' -Raw | ConvertFrom-Json; "
                     "$m = @(Get-SaiAccountDescriptors (Resolve-SaiAccountsEngine) | "
                     "Where-Object { $_.locator -and "
                     "((Split-Path -Leaf $_.locator) -eq (Split-Path -Leaf '%s')) }); "
                     "if ($m.Count -gt 0) { 'match' } else { 'none' }"
                     % (REGISTRY, locator))
            if out[1] == "match":
                collapsed += 1
        self.assertTrue(collapsed, "no canonical Claude account matched a local profile")
        payload = self.federated()
        claude = [p for p in payload["profiles"]
                  if p["Origin"] == "shared" and "claude" in (p["AccountId"] or "")]
        self.assertEqual(len(claude), collapsed)
        self.assertFalse([p for p in payload["profiles"]
                          if p["Origin"] == "local" and p["Id"].startswith("claude")])

    def test_merging_never_keys_on_display_name(self):
        """Identity is provider + locator. A name is a label, never a key."""
        body = re.search(r"function Get-AccountsIdentityKey.*?\n}", ACCOUNTS_CODE,
                         re.DOTALL).group(0)
        for forbidden in ("display_name", "label", "compact", "name"):
            self.assertNotIn(forbidden, body.lower())
        # And the reconciliation path only ever compares those identity keys.
        merge = re.search(r"function Expand-AccountProfiles.*?\n}", ACCOUNTS_CODE,
                          re.DOTALL).group(0)
        self.assertIn("$claimed.ContainsKey($local)", merge)
        self.assertIn("$account.identity", merge)

    def test_hidden_and_non_enabled_accounts_leave_the_menu(self):
        rc, out, err = ps(
            "$a = @([pscustomobject]@{ account_id='x:profile-dir:0000000000000001'; "
            "provider_id='claude'; display_name='Same Name'; compact='X1'; "
            "state='ENABLED'; hidden=$false; launchable=$true; "
            "locator='C:\\profiles\\visible'; identity='claude|c:\\profiles\\visible' },"
            "[pscustomobject]@{ account_id='x:profile-dir:0000000000000002'; "
            "provider_id='claude'; display_name='Same Name'; compact='X2'; "
            "state='ENABLED'; hidden=$true; launchable=$false; "
            "locator='C:\\profiles\\hidden'; identity='claude|c:\\profiles\\hidden' },"
            "[pscustomobject]@{ account_id='x:profile-dir:0000000000000003'; "
            "provider_id='claude'; display_name='Same Name'; compact='X3'; "
            "state='FROZEN'; hidden=$false; launchable=$false; "
            "locator='C:\\profiles\\frozen'; identity='claude|c:\\profiles\\frozen' });"
            "$t = ([pscustomobject]@{ id='shared-claude'; label='Claude'; "
            "expand_accounts=$true; provider='claude'; title_suffix='YOLO' });"
            "$p = @(Expand-AccountProfiles -Profiles @($t) -Engine 'X' -Accounts $a);"
            "@($p).Count")
        self.assertEqual(rc, 0, err)
        # Three accounts, one identical display name: only the launchable one
        # reaches the launch surface.
        self.assertEqual(out, "1")

    def test_federated_account_keeps_the_provider_template_icon(self):
        rc, out, err = ps(
            "$a = @([pscustomobject]@{ account_id='claude:profile-dir:0000000000000001'; "
            "provider_id='claude'; display_name='Claude 1'; state='ENABLED'; hidden=$false; launchable=$true; "
            "locator='C:\\profiles\\visible'; identity='claude|c:\\profiles\\visible' });"
            "$t = [pscustomobject]@{ id='shared-claude'; label='Claude'; expand_accounts=$true; "
            "provider='claude'; icon='%LOCALAPPDATA%\\AnthropicClaude\\claude.exe' };"
            "$p = @(Expand-AccountProfiles -Profiles @($t) -Engine 'X' -Accounts $a);"
            "$p[0].icon")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, r"%LOCALAPPDATA%\AnthropicClaude\claude.exe")

    def test_a_local_profile_collapsed_into_a_disabled_account_leaves_the_menu(self):
        """The live cascade kept offering the disabled Codex 3.

        A local profile whose identity a canonical account claims IS that
        account. Collapsing the two is what keeps one physical account from
        being listed twice, so the account's launchable flag has to decide for
        the collapsed slot too: otherwise the menu keeps an entry whose only
        possible outcome is a lifecycle refusal.
        """
        rc, out, err = ps(
            "$a = @([pscustomobject]@{ account_id='codex:profile-dir:0'; "
            "provider_id='codex'; display_name='Codex 3'; compact='CX3'; "
            "state='DISABLED'; hidden=$false; launchable=$false; "
            "locator='C:\\profiles\\cx3'; identity='codex|c:\\profiles\\cx3' });"
            "$p = [pscustomobject]@{ id='codex-3-free'; label='Codex 3 free'; "
            "command='codex'; arguments=@(); environment=([ordered]@{ "
            "CODEX_HOME='C:\\profiles\\cx3' }); provider='codex' };"
            "@(Expand-AccountProfiles -Profiles @($p) -Engine 'X' -Accounts $a).Count")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "0")

    def test_federation_on_without_an_engine_fails_closed(self):
        rc, payload, stderr = run_selftest(federation="on", env={
            "SAI_ACCOUNTS_EXE": str(REPO / "Scripts" / "consoles" / "ACCOUNTS.ps1")})
        # The canonical install still resolves on this machine, so the honest
        # assertion is the one below: 'on' never silently degrades to 'off'.
        if payload:
            self.assertIsNotNone(payload["federation"]["engine"])
        else:
            self.assertIn("control plane is not installed", stderr)

    def test_installer_and_launcher_share_one_expansion(self):
        self.assertIn("ACCOUNTS.ps1", INSTALLER_SOURCE)
        self.assertIn("Expand-AccountProfiles", INSTALLER_SOURCE)
        self.assertIn("Get-SaiAccountDescriptors", INSTALLER_SOURCE)
        self.assertIn("ACCOUNTS.ps1", LAUNCHER_SOURCE)
        self.assertIn("Expand-AccountProfiles", LAUNCHER_SOURCE)

    def test_interleaved_canonical_order_is_respected_across_providers(self):
        """Canonical account presentation order (even interleaved across
        providers like AG2, CL2, CX1, AG1, CL1) must drive the federated
        projection rather than grouping by static local profiles or provider
        templates."""
        rc, out, err = ps(
            "$reg = Get-Content %s -Raw -Encoding UTF8 | ConvertFrom-Json; "
            "$u = $env:USERNAME; "
            "$h = $env:USERPROFILE; "
            "$a = @("
            "[pscustomobject]@{ account_id='antigravity:windows-user:0000000000000002'; provider_id='antigravity'; display_name='Antigravity 2'; compact='AG2'; state='ENABLED'; hidden=$false; launchable=$true; locator='ag_b'; identity=(Get-AccountsIdentityKey 'antigravity' 'ag_b') },"
            "[pscustomobject]@{ account_id='claude:profile-dir:0000000000000002'; provider_id='claude'; display_name='Claude 2'; compact='CL2'; state='ENABLED'; hidden=$false; launchable=$true; locator=\"$h\\.claude-account2\"; identity=(Get-AccountsIdentityKey 'claude' \"$h\\.claude-account2\") },"
            "[pscustomobject]@{ account_id='codex:profile-dir:0000000000000001'; provider_id='codex'; display_name='Codex 1'; compact='CX1'; state='ENABLED'; hidden=$false; launchable=$true; locator=\"$h\\.codex\"; identity=(Get-AccountsIdentityKey 'codex' \"$h\\.codex\") },"
            "[pscustomobject]@{ account_id='antigravity:windows-user:0000000000000001'; provider_id='antigravity'; display_name='Antigravity 1'; compact='AG1'; state='ENABLED'; hidden=$false; launchable=$true; locator=$u; identity=(Get-AccountsIdentityKey 'antigravity' $u) },"
            "[pscustomobject]@{ account_id='claude:profile-dir:0000000000000001'; provider_id='claude'; display_name='Claude 1'; compact='CL1'; state='ENABLED'; hidden=$false; launchable=$true; locator=\"$h\\.claude\"; identity=(Get-AccountsIdentityKey 'claude' \"$h\\.claude\") },"
            "[pscustomobject]@{ account_id='codex:profile-dir:0000000000000002'; provider_id='codex'; display_name='Codex 2'; compact='CX2'; state='ENABLED'; hidden=$false; launchable=$true; locator=\"$h\\.codex-account2\"; identity=(Get-AccountsIdentityKey 'codex' \"$h\\.codex-account2\") },"
            "[pscustomobject]@{ account_id='codex:profile-dir:0000000000000003'; provider_id='codex'; display_name='Codex 3'; compact='CX3'; state='DISABLED'; hidden=$false; launchable=$false; locator=\"$h\\.codex-account3free\"; identity=(Get-AccountsIdentityKey 'codex' \"$h\\.codex-account3free\") },"
            "[pscustomobject]@{ account_id='antigravity:windows-user:0000000000000003'; provider_id='antigravity'; display_name='AG3 Hidden'; compact='AG3'; state='ENABLED'; hidden=$true; launchable=$false; locator='ag_c'; identity=(Get-AccountsIdentityKey 'antigravity' 'ag_c') }"
            "); "
            "$expanded = @(Expand-AccountProfiles -Profiles @($reg.profiles) -Engine 'C:\\ProgramData\\SAI\\Accounts\\bin\\sai-accounts.exe' -Accounts $a); "
            "@($expanded | ForEach-Object { [pscustomobject]@{ id=$_.id; label=$_.label; origin=$_.origin; account_id=$(if ($_.PSObject.Properties['account_id']) { $_.account_id } else { $null }) } }) | ConvertTo-Json -Compress"
            % ps_single(REGISTRY))
        self.assertEqual(rc, 0, err)
        rows = json.loads(out)
        shared_ids = [r["account_id"] for r in rows if r["origin"] == "shared"]
        self.assertEqual(shared_ids, [
            "antigravity:windows-user:0000000000000002",
            "claude:profile-dir:0000000000000002",
            "codex:profile-dir:0000000000000001",
            "antigravity:windows-user:0000000000000001",
            "claude:profile-dir:0000000000000001",
            "codex:profile-dir:0000000000000002",
        ], rows)
        # Local non-account tools stay deterministically after the canonical accounts
        local_ids = [r["id"] for r in rows if r["origin"] == "local"]
        self.assertEqual(local_ids, ["zcode", "cline", "opencode"], rows)
        # Hidden and disabled entries (and their collapsed local profiles) are absent
        all_ids = [r["id"] for r in rows]
        self.assertNotIn("codex-3-free", all_ids)
        self.assertFalse([i for i in all_ids if "0000000000000003" in i], all_ids)

class LocaleLabelTests(unittest.TestCase):
    def test_registry_carries_no_hardcoded_menu_label(self):
        self.assertNotIn("menu_label", REGISTRY_DOC)
        self.assertNotIn("Открыть", REGISTRY.read_text(encoding="utf-8"))
        self.assertNotIn("Открыть", INSTALLER_SOURCE)

    def test_menu_label_follows_the_locale(self):
        for locale, expected in (("en", "Open in"), ("ru", "Открыть в"),
                                 ("et", "Ava rakenduses"), ("ded", "Открыть в")):
            rc, out, err = ps("Get-ConsolesMenuLabel '%s'" % REPO, env={"SAITULS_LANG": locale})
            self.assertEqual(rc, 0, err)
            self.assertEqual(out, expected, "locale %s" % locale)

    def test_unknown_locale_falls_back_to_english(self):
        rc, out, err = ps("Get-ConsolesMenuLabel '%s'" % REPO, env={"SAITULS_LANG": "zz"})
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "Open in")

    def test_every_locale_bundle_carries_the_key(self):
        for path in sorted((REPO / "i18n" / "strings").glob("strings.*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("menu.open_in", doc, path.name)
            self.assertTrue(doc["menu.open_in"].strip(), path.name)

class TemplateSafetyTests(unittest.TestCase):
    def test_account_templates_carry_no_command_and_no_arguments(self):
        templates = [p for p in REGISTRY_DOC["profiles"] if is_template(p)]
        self.assertTrue(templates)
        for template in templates:
            self.assertNotIn("command", template)
            self.assertNotIn("arguments", template)
            self.assertNotIn("environment", template)
            self.assertIn("provider", template)

    def test_a_template_declaring_a_command_is_refused(self):
        doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
        for profile in doc["profiles"]:
            if not is_template(profile):
                continue
            profile["command"] = "calc.exe"
        with tempfile.TemporaryDirectory(prefix="saituls-console-tpl-") as tmp:
            bad = Path(tmp) / "consoles.json"
            bad.write_text(json.dumps(doc), encoding="utf-8")
            probe = Path(tmp) / "CONSOLES.ps1"
            probe.write_text(LAUNCHER_SOURCE, encoding="utf-8")
            (Path(tmp) / "ACCOUNTS.ps1").write_text(ACCOUNTS_SOURCE, encoding="utf-8")
            completed = subprocess.run(
                ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(probe), "-SelfTest"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("must not carry a command",
                          (completed.stderr or "") + (completed.stdout or ""))

    def test_federation_code_carries_no_credential_material(self):
        code = ACCOUNTS_CODE.lower()
        self.assertIn("expand-accountprofiles", code)  # sanity: code survived
        for forbidden in ("password", "token", "cookie", "savecred", "netonly",
                          "credread", "passkey", "runas", "authorization"):
            self.assertNotIn(forbidden, code,
                             "ACCOUNTS.ps1 code must not contain %r" % forbidden)

    def test_launcher_never_gains_elevation_through_federation(self):
        self.assertNotIn("RunAs", LAUNCHER_CODE)
        self.assertNotIn("RunAs", strip_powershell_comments(INSTALLER_SOURCE)
                         .replace("Global\\SAITULS_REGISTRY_MUTATION", ""))

class EnvironmentMapTests(unittest.TestCase):
    """A federated profile's environment is built as a dictionary, not parsed
    from JSON, and the two shapes must read the same way."""

    def test_empty_dictionary_environment_yields_no_variables(self):
        # PowerShell truthiness trap: an empty IDictionary is TRUE, and its
        # PSObject properties (Count, Keys, Values, IsReadOnly...) are not
        # environment variables. Enumerating them would set seven nonsense vars.
        rc, out, err = ps("$p = [pscustomobject]@{ environment = ([ordered]@{}) }; "
                         "$v = Get-ConsoleEnvironmentValue $p 'HOME'; "
                         "if ($null -eq $v) { 'none' } else { \"got:$v\" }")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "none")

    def test_dictionary_environment_is_read_like_a_json_one(self):
        rc, out, err = ps(
            "$p = [pscustomobject]@{ environment = "
            "([ordered]@{ CLAUDE_CONFIG_DIR = '%USERPROFILE%\\.claude' }) }; "
            "Get-ConsoleEnvironmentValue $p 'CLAUDE_CONFIG_DIR'")
        self.assertEqual(rc, 0, err)
        # Raw value; the caller is what expands it, exactly as for JSON profiles.
        self.assertEqual(out, "%USERPROFILE%\\.claude")

    def test_local_profile_identity_expands_the_locator(self):
        rc, out, err = ps(
            "$p = [pscustomobject]@{ provider = 'claude'; "
            "environment = ([ordered]@{ CLAUDE_CONFIG_DIR = '%USERPROFILE%\\.claude' }) }; "
            "Get-LocalProfileIdentity $p")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "claude|" + os.path.join(
            os.environ["USERPROFILE"], ".claude").lower())

    def test_launcher_resolves_a_dictionary_environment_as_a_dictionary(self):
        self.assertIn("$env -is [System.Collections.IDictionary]", LAUNCHER_SOURCE)


# ------------------------------------------------- Explorer launch boundary
#
# A click in Explorer has no terminal to read. Before the boundary existed, a
# preflight throw printed its error into the console Explorer had just created,
# and that console closed immediately: the operator saw a black window flash and
# nothing else. These cases pin the repaired contract -- a nonzero exit plus one
# durable, sanitized diagnostic line -- with the modal dialog suppressed, since
# a test has nobody to dismiss it.


def ps_single(text):
    """A PowerShell single-quoted literal; the only quoting that survives a path."""
    return "'" + str(text).replace("'", "''") + "'"


def psraw(script, env=None):
    """Plain PowerShell, without dot-sourcing the federation module."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    completed = subprocess.run(
        ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), env=merged, timeout=180)
    return completed.returncode, (completed.stdout or "").strip(), completed.stderr or ""


STUB_CACHE = {}
STUB_DIR = []


def build_stub_engine(name="stub-engine"):
    """A minimal stand-in for an engine or a CLI.

    It records the argv it was REALLY started with and prints whatever envelope
    the test asks for, so quoting and the launch verdict are proved against a
    real child process instead of against the adapter's own say-so. The Windows
    .NET compiler is the same precondition the control-plane suite has: a
    missing one is a FAIL, never a SKIP.

    Compiled once per name and cached outside every per-test temporary
    directory, because a class that compiles a stub in setUp would pay for it
    once per test method.
    """
    if name in STUB_CACHE and os.path.exists(STUB_CACHE[name]):
        return STUB_CACHE[name]
    if not STUB_DIR:
        STUB_DIR.append(tempfile.mkdtemp(prefix="saituls-stub-"))
        atexit.register(shutil.rmtree, STUB_DIR[0], ignore_errors=True)
    directory = STUB_DIR[0]
    csc = None
    for framework in ("Framework64", "Framework"):
        candidate = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                                 "Microsoft.NET", framework, "v4.0.30319", "csc.exe")
        if os.path.exists(candidate):
            csc = candidate
            break
    if csc is None:
        raise AssertionError("csc.exe not found: a missing compiler is a FAIL, never a SKIP")
    source = Path(directory) / (name + ".cs")
    exe = Path(directory) / (name + ".exe")
    source.write_text(r'''
using System;
using System.IO;
class StubEngine {
    static int Main(string[] args) {
        string argFile = Environment.GetEnvironmentVariable("STUB_ARGV_FILE");
        if (!string.IsNullOrEmpty(argFile)) {
            var sb = new System.Text.StringBuilder();
            for (int i = 0; i < args.Length; i++) {
                if (i > 0) sb.Append("\n");
                sb.Append(args[i]);
            }
            File.WriteAllText(argFile, sb.ToString());
        }
        string stdout = Environment.GetEnvironmentVariable("STUB_STDOUT");
        if (!string.IsNullOrEmpty(stdout)) Console.WriteLine(stdout.Replace("\\n", "\n"));
        string stderr = Environment.GetEnvironmentVariable("STUB_STDERR");
        if (!string.IsNullOrEmpty(stderr)) Console.Error.WriteLine(stderr);
        int code = 0;
        int.TryParse(Environment.GetEnvironmentVariable("STUB_EXIT"), out code);
        return code;
    }
}
''', encoding="utf-8")
    built = subprocess.run([csc, "-nologo", "-target:exe", "-out:" + str(exe), str(source)],
                           capture_output=True, text=True, timeout=180)
    if built.returncode != 0 or not exe.exists():
        raise AssertionError("stub did not compile:\n" + built.stdout + built.stderr)
    STUB_CACHE[name] = str(exe)
    return str(exe)


class AccountLaunchVerdictTests(unittest.TestCase):
    """A valid JSON envelope is not a successful launch.

    SaiAccounts.cs is repaired to exit 0 only on status == 'launched' (proved by
    test_sai_accounts.py), but the adapter must not depend on the engine it
    happens to be talking to being the repaired one. An installed or vendored
    binary that answers launch_failed and still exits 0 is the version skew this
    defence exists for, and it fails here.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="saituls-verdict-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.stub = build_stub_engine()

    def envelope(self, status, extra=""):
        return '{"schema":"sai.accounts/launch/1","status":"%s"%s}' % (status, extra)

    def verdict(self, stdout=None, exit_code=0, stderr=None, workdir=None):
        target = workdir or self.tmp
        argv_file = os.path.join(self.tmp, "argv.txt")
        if os.path.exists(argv_file):
            os.remove(argv_file)
        env = {"STUB_ARGV_FILE": argv_file, "STUB_EXIT": str(exit_code)}
        if stdout is not None:
            env["STUB_STDOUT"] = stdout
        if stderr is not None:
            env["STUB_STDERR"] = stderr
        script = ("$v = Invoke-AccountsEngineLaunch -Engine %s "
                  "-Arguments @('launch','x:profile-dir:0','--workdir',%s) "
                  "-WorkDir %s -TimeoutMs 20000; $v | ConvertTo-Json -Compress"
                  % (ps_single(self.stub), ps_single(target), ps_single(target)))
        rc, out, err = ps(script, env=env)
        self.assertEqual(rc, 0, err)
        result = json.loads(out[out.find("{"):])
        if os.path.exists(argv_file):
            result["argv"] = Path(argv_file).read_text(encoding="utf-8").split("\n")
        return result

    def test_exit_0_with_status_launched_is_success(self):
        verdict = self.verdict(self.envelope("launched", ',"pid":4242'))
        self.assertTrue(verdict["Ok"], verdict)
        self.assertEqual(verdict["Pid"], 4242)
        self.assertEqual(verdict["Category"], "LAUNCHED")

    def test_exit_0_with_launch_failed_is_a_failure(self):
        verdict = self.verdict(self.envelope("launch_failed",
                                             ',"error":"CreateProcess failed: 2"'))
        self.assertFalse(verdict["Ok"], verdict)
        self.assertEqual(verdict["Status"], "launch_failed")
        self.assertIn("launch_failed", verdict["Reason"])

    def test_exit_0_with_workdir_unavailable_is_a_failure(self):
        verdict = self.verdict(self.envelope("WORKDIR_UNAVAILABLE", ',"reason":"NOT_FOUND"'))
        self.assertFalse(verdict["Ok"], verdict)
        self.assertIn("NOT_FOUND", verdict["Reason"])

    def test_a_nonzero_exit_with_status_launched_is_never_silent_success(self):
        verdict = self.verdict(self.envelope("launched", ',"pid":1'), exit_code=3)
        self.assertFalse(verdict["Ok"], verdict)
        self.assertEqual(verdict["Category"], "ACCOUNT_ENGINE_INCONSISTENT")
        self.assertIn("3", verdict["Reason"])

    def test_every_structured_refusal_is_a_failure(self):
        for status in ("launch_failed", "WORKDIR_UNAVAILABLE", "CONTEXT_OFFLINE",
                       "LIFECYCLE_FORBIDDEN", "BACKEND_UNSUPPORTED",
                       "PROFILE_LOCATOR_MISSING", "PROVIDER_UNSUPPORTED",
                       "ACCOUNT_NOT_FOUND"):
            with self.subTest(status=status):
                verdict = self.verdict(self.envelope(status))
                self.assertFalse(verdict["Ok"], verdict)
                self.assertEqual(verdict["Status"], status)

    def test_an_engine_that_says_nothing_is_not_a_success(self):
        verdict = self.verdict(None, stderr="engine exploded")
        self.assertFalse(verdict["Ok"], verdict)
        self.assertEqual(verdict["Category"], "ACCOUNT_ENGINE_NO_ENVELOPE")
        self.assertIn("engine exploded", verdict["Reason"])

    def test_a_foreign_envelope_is_refused(self):
        verdict = self.verdict('{"schema":"sai.accounts/mutate/1","status":"ok"}')
        self.assertFalse(verdict["Ok"], verdict)
        self.assertEqual(verdict["Category"], "ACCOUNT_ENGINE_SCHEMA_MISMATCH")

    def test_a_workdir_with_spaces_arrives_as_one_argument(self):
        project = os.path.join(self.tmp, "My Big Project 2")
        os.makedirs(project, exist_ok=True)
        verdict = self.verdict(self.envelope("launched", ',"pid":7'), workdir=project)
        self.assertTrue(verdict["Ok"], verdict)
        self.assertEqual(verdict["argv"],
                         ["launch", "x:profile-dir:0", "--workdir", project])

    def test_a_unicode_workdir_arrives_intact(self):
        project = os.path.join(self.tmp, "\u043f\u0440\u043e\u0435\u043a\u0442 \u00dcn\u00efc\u00f8d\u00e9")
        os.makedirs(project, exist_ok=True)
        verdict = self.verdict(self.envelope("launched", ',"pid":8'), workdir=project)
        self.assertTrue(verdict["Ok"], verdict)
        self.assertEqual(verdict["argv"][3], project)

    def test_a_drive_root_never_escapes_its_own_quote(self):
        rc, out, err = ps("ConvertTo-AccountsArgument 'C:\\'")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "C:\\")


class ExplorerLaunchBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="saituls-explorer-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = os.path.join(self.tmp, "consoles-launch.log")
        self.windir = os.environ.get("WINDIR", r"C:\Windows")

    def launch(self, *extra, env=None, launcher=None):
        merged = dict(os.environ)
        merged["SAITULS_CONSOLE_LOG"] = self.log
        if env:
            merged.update(env)
        return subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(launcher or LAUNCHER), "-FromExplorer", "-NoErrorDialog"]
            + list(extra),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO), env=merged, timeout=180)

    def diagnostics(self):
        if not os.path.exists(self.log):
            return []
        text = Path(self.log).read_text(encoding="utf-8-sig")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def test_a_broken_launch_exits_nonzero_with_a_durable_reason(self):
        completed = self.launch("-Profile", "not-a-profile", "-WorkDir", self.windir,
                                "-Federation", "off")
        self.assertNotEqual(completed.returncode, 0)
        rows = self.diagnostics()
        self.assertTrue(rows, "no durable diagnostic was written")
        self.assertEqual(rows[-1]["outcome"], "failed")
        self.assertEqual(rows[-1]["category"], "PROFILE_UNKNOWN")
        self.assertEqual(rows[-1]["source"], "explorer")
        self.assertEqual(rows[-1]["profile"], "not-a-profile")
        self.assertTrue(rows[-1]["launcher"].lower().endswith("consoles.ps1"), rows[-1])

    def test_the_reason_is_not_only_printed_into_a_closing_window(self):
        """The console is already gone; the message must survive it."""
        completed = self.launch("-Profile", "not-a-profile", "-WorkDir", self.windir,
                                "-Federation", "off")
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("unknown console profile", completed.stdout)
        self.assertIn("unknown console profile", self.diagnostics()[-1]["reason"])

    def test_a_stale_account_entry_is_named_as_stale(self):
        completed = self.launch("-Profile", "account-claude-profile-dir-0000000000000000",
                                "-WorkDir", self.windir)
        self.assertNotEqual(completed.returncode, 0)
        row = self.diagnostics()[-1]
        self.assertEqual(row["category"], "STALE_EXPLORER_MENU_ENTRY")
        self.assertIn("rebuild the menu", row["reason"].lower())

    def test_an_unusable_project_folder_is_durable(self):
        completed = self.launch("-Profile", "opencode",
                                "-WorkDir", os.path.join(self.tmp, "no-such-folder"),
                                "-Federation", "off")
        self.assertNotEqual(completed.returncode, 0)
        row = self.diagnostics()[-1]
        self.assertEqual(row["category"], "SCRIPT_PREFLIGHT_EXCEPTION")
        self.assertIn("Project directory not found", row["reason"])

    def test_a_local_console_launch_is_recorded_as_launched(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        shutil.copy(build_stub_engine(), os.path.join(bindir, "stubconsole.exe"))
        registry = os.path.join(self.tmp, "consoles")
        os.makedirs(registry, exist_ok=True)
        shutil.copy(str(LAUNCHER), registry)
        shutil.copy(str(ACCOUNTS_MODULE), registry)
        doc = json.loads(REGISTRY.read_text(encoding="utf-8-sig"))
        doc["profiles"] = [{
            "id": "stubconsole", "label": "Stub console", "enabled": True,
            "command": "stubconsole", "arguments": ["--cwd", "{workdir}"],
            "environment": {}, "working_directory_mode": "ask",
            "requires_admin": False, "title_suffix": "Stub",
            "description": "stub CLI"}]
        (Path(registry) / "consoles.json").write_text(json.dumps(doc), encoding="utf-8")
        project = os.path.join(self.tmp, "project")
        os.makedirs(project, exist_ok=True)
        argv_file = os.path.join(self.tmp, "argv.txt")
        completed = self.launch(
            "-Profile", "stubconsole", "-WorkDir", project, "-Federation", "off",
            launcher=os.path.join(registry, "CONSOLES.ps1"),
            env={"PATH": bindir + os.pathsep + os.environ["PATH"],
                 "STUB_ARGV_FILE": argv_file, "STUB_EXIT": "0"})
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        outcomes = [row["outcome"] for row in self.diagnostics()]
        self.assertIn("launched", outcomes)
        self.assertIn("exited", outcomes)
        self.assertEqual(self.diagnostics()[-1]["exit_code"], 0)
        self.assertEqual(Path(argv_file).read_text(encoding="utf-8"),
                         "--cwd\n" + project)

    def test_the_diagnostic_never_carries_a_secret(self):
        completed = self.launch("-Profile", "not-a-profile", "-WorkDir", self.windir,
                                "-Federation", "off",
                                env={"OPENAI_API_KEY": "sk-CANARY-abcdef123456"})
        self.assertNotEqual(completed.returncode, 0)
        raw = Path(self.log).read_text(encoding="utf-8-sig")
        self.assertNotIn("sk-CANARY", raw)


class ConsolesMenuProjectionTests(unittest.TestCase):
    """The cascade is a projection of consoles.json plus the canonical
    accounts, and a projection that is never re-derived is a stale menu.

    Every run here writes to a disposable HKCU class root, so a projection or
    drift assertion never has to touch the menu the operator is actually using.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="saituls-cascade-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = ("HKCU:\\Software\\Classes\\SaitulsConsolesTest_" + os.urandom(4).hex())
        self.addCleanup(self.remove_root)
        self.stub = build_stub_engine()

    def remove_root(self):
        psraw("if (Test-Path -LiteralPath %s) { Remove-Item -LiteralPath %s -Recurse -Force }"
              % (ps_single(self.root), ps_single(self.root)))

    def accounts(self, entries):
        rows = []
        for index, (name, state, hidden) in enumerate(entries, start=1):
            rows.append(
                '{"account_id":"antigravity:windows-user:%016x","provider_id":"antigravity",'
                '"display_name":"%s","compact_label":"AG%d","operational_state":"%s",'
                '"hidden":%s,"provider_metadata":{"windows_user":"w%d"}}'
                % (index, name, index, state, "true" if hidden else "false", index))
        return '{"schema":"sai.accounts/list/1","accounts":[%s]}' % ",".join(rows)

    def install(self, accounts=None, verify=False):
        env = dict(os.environ)
        if accounts is not None:
            env.update({"SAI_ACCOUNTS_EXE": self.stub, "STUB_STDOUT": accounts,
                        "STUB_EXIT": "0"})
        invocation = ("& %s -RegistryClassesRoot %s -GatewayDir %s -Federation on"
                      % (ps_single(str(INSTALLER)), ps_single(self.root),
                         ps_single(os.path.join(self.tmp, "gateway"))))
        if verify:
            invocation += " -VerifyOnly"
        # ErrorRecord formatting wraps paths at the host's console width.
        # Capture the actual message so the literal path assertions stay exact.
        command = ("$ErrorActionPreference='Stop'; try { %s } catch { "
                   "[Console]::Error.WriteLine($_.Exception.Message); exit 1 }" % invocation)
        args = ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-Command", command]
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(REPO), env=env, timeout=300)

    def projection(self, shell_root="Directory"):
        script = (
            "$base = %s + '\\%s\\shell\\SaitulsAgents\\shell'; "
            "if (-not (Test-Path -LiteralPath $base)) { '' } else { "
            "(Get-ChildItem -LiteralPath $base | ForEach-Object { "
            "$_.PSChildName + '=' + (Get-Item -LiteralPath ($_.PSPath + '\\command')).GetValue('') }) -join \"`n\" }"
            % (ps_single(self.root), shell_root))
        rc, out, err = psraw(script)
        self.assertEqual(rc, 0, err)
        items = {}
        for line in out.splitlines():
            if "=" in line:
                key, command = line.split("=", 1)
                items[key] = command
        return items

    def test_a_new_account_appears_in_all_three_shell_roots(self):
        """Account created -> the Explorer cascade gains it, with no manual step."""
        third = "0000000000000003"
        self.install(self.accounts([("Antigravity 1", "ENABLED", False),
                                    ("Antigravity 2", "ENABLED", False)]))
        self.assertFalse([k for k in self.projection() if third in k], self.projection())
        self.install(self.accounts([("Antigravity 1", "ENABLED", False),
                                    ("Antigravity 2", "ENABLED", False),
                                    ("AG3", "ENABLED", False)]))
        for shell_root in ("Directory", "Directory\\Background", "Drive"):
            with self.subTest(shell_root=shell_root):
                items = self.projection(shell_root)
                self.assertEqual(len([k for k in items if third in k]), 1, items)
                self.assertEqual(len([k for k in items if "account-antigravity" in k]), 3, items)
        self.assertEqual(len(self.projection()), 3 + self.local_count())

    def local_count(self):
        """Every concrete profile still ships, plus the local Antigravity one:
        its locator is the operator's user name, which none of the synthetic
        canonical accounts claim."""
        rows = [p for p in REGISTRY_DOC["profiles"] if not p.get("expand_accounts")]
        return len({p["id"] for p in rows if p.get("enabled", True)})

    def test_a_hidden_account_leaves_the_cascade(self):
        self.install(self.accounts([("AG1", "ENABLED", False), ("AG2", "ENABLED", False)]))
        self.assertEqual(len([k for k in self.projection() if "account-" in k]), 2)
        self.install(self.accounts([("AG1", "ENABLED", False), ("AG2", "ENABLED", True)]))
        items = self.projection()
        self.assertEqual(len([k for k in items if "account-" in k]), 1, items)

    def test_a_disabled_account_leaves_the_cascade(self):
        self.install(self.accounts([("AG1", "ENABLED", False), ("AG2", "DISABLED", False)]))
        items = self.projection()
        self.assertEqual(len([k for k in items if "account-" in k]), 1, items)

    def test_a_deleted_account_is_removed_from_every_shell_root(self):
        self.install(self.accounts([("AG1", "ENABLED", False), ("AG2", "ENABLED", False)]))
        self.install(self.accounts([("AG1", "ENABLED", False)]))
        for shell_root in ("Directory", "Directory\\Background", "Drive"):
            with self.subTest(shell_root=shell_root):
                items = self.projection(shell_root)
                self.assertEqual(len([k for k in items if "account-" in k]), 1, items)

    def test_a_shared_entry_is_a_hidden_bootstrap_with_the_boundary_armed(self):
        """T-264: the verb is the native gateway; it decides hidden (shared
        bootstrap) vs visible (local agent) from the resolved origin, and
        always arms the CONSOLES.ps1 boundary with -FromExplorer."""
        self.install(self.accounts([("AG1", "ENABLED", False)]))
        items = self.projection()
        shared = [(k, v) for k, v in items.items() if "account-" in k]
        self.assertTrue(shared, items)
        for _key, command in items.items():
            self.assertIn("SaiConsoleLauncher.exe", command)
            self.assertNotIn("-WindowStyle", command)
        gateway = (CONSOLES / "SaiConsoleLauncher.cs").read_text(encoding="utf-8")
        self.assertIn('psi.CreateNoWindow = origin == "shared";', gateway)
        self.assertIn('(origin == "shared" ? " -WindowStyle Hidden" : "")', gateway)
        self.assertIn('" -FromExplorer"', gateway)

    def test_a_stale_install_is_detected_and_repaired(self):
        """A repository that moved leaves the verb pointing at the old path."""
        self.install(self.accounts([("AG1", "ENABLED", False)]))
        key = sorted(self.projection())[0]
        stale = ('"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
                 '-NoLogo -NoProfile -ExecutionPolicy Bypass -File '
                 '"C:\\a-repo-that-moved\\Scripts\\consoles\\CONSOLES.ps1" '
                 '-Profile %s -WorkDir "%%V."' % key.split("_", 1)[1])
        rc, _out, err = psraw(
            "Set-ItemProperty -LiteralPath %s -Name '(default)' -Value %s"
            % (ps_single(self.root + "\\Directory\\shell\\SaitulsAgents\\shell\\" + key
                         + "\\command"), ps_single(stale)))
        self.assertEqual(rc, 0, err)
        broken = self.install(self.accounts([("AG1", "ENABLED", False)]), verify=True)
        self.assertNotEqual(broken.returncode, 0, "a stale launcher path was not detected")
        combined = (broken.stdout or "") + (broken.stderr or "")
        self.assertIn(r"C:\a-repo-that-moved", combined)
        self.assertIn(str(LAUNCHER), combined)
        repaired = self.install(self.accounts([("AG1", "ENABLED", False)]))
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        verified = self.install(self.accounts([("AG1", "ENABLED", False)]), verify=True)
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertIn("SaiConsoleLauncher.exe", self.projection()[key])

    def test_the_installed_command_names_the_current_launcher_and_carries_the_dot_trick(self):
        """T-264: the verb is the gateway + projection id + "%V" -- no baked
        launcher path; a drive root is the gateway's raw parser's job."""
        self.install(self.accounts([("AG1", "ENABLED", False)]))
        gateway = os.path.join(self.tmp, "gateway", "SaiConsoleLauncher.exe")
        for key, command in self.projection().items():
            self.assertEqual(command, '"%s" %s "%%V"' % (gateway, key.split("_", 1)[1]))
            self.assertNotIn(str(LAUNCHER), command)

    def test_reordered_interleaved_accounts_update_all_three_shell_roots(self):
        """Reordering canonical accounts into an interleaved order updates the
        numeric menu keys and MUIVerb labels in Directory, Directory\\Background
        and Drive, removing stale old numbering while keeping commands bound to
        the exact account_ids."""
        home = os.environ["USERPROFILE"].replace("\\", "\\\\")
        user = os.environ["USERNAME"]
        initial_json = (
            '{"schema":"sai.accounts/list/1","accounts":['
            '{"account_id":"antigravity:windows-user:0000000000000001","provider_id":"antigravity","display_name":"Antigravity 1","compact_label":"AG1","operational_state":"ENABLED","hidden":false,"provider_metadata":{"windows_user":"%s"}},'
            '{"account_id":"claude:profile-dir:0000000000000001","provider_id":"claude","display_name":"Claude 1","compact_label":"CL1","operational_state":"ENABLED","hidden":false,"provider_metadata":{"profile_locator":"%s\\\\.claude"}},'
            '{"account_id":"codex:profile-dir:0000000000000001","provider_id":"codex","display_name":"Codex 1","compact_label":"CX1","operational_state":"ENABLED","hidden":false,"provider_metadata":{"profile_locator":"%s\\\\.codex"}},'
            '{"account_id":"antigravity:windows-user:0000000000000002","provider_id":"antigravity","display_name":"Antigravity 2","compact_label":"AG2","operational_state":"ENABLED","hidden":false,"provider_metadata":{"windows_user":"ag_b"}},'
            '{"account_id":"claude:profile-dir:0000000000000002","provider_id":"claude","display_name":"Claude 2","compact_label":"CL2","operational_state":"ENABLED","hidden":false,"provider_metadata":{"profile_locator":"%s\\\\.claude-account2"}}'
            ']}' % (user, home, home, home)
        )
        reordered_json = (
            '{"schema":"sai.accounts/list/1","accounts":['
            '{"account_id":"antigravity:windows-user:0000000000000002","provider_id":"antigravity","display_name":"Antigravity 2","compact_label":"AG2","operational_state":"ENABLED","hidden":false,"provider_metadata":{"windows_user":"ag_b"}},'
            '{"account_id":"claude:profile-dir:0000000000000002","provider_id":"claude","display_name":"Claude 2","compact_label":"CL2","operational_state":"ENABLED","hidden":false,"provider_metadata":{"profile_locator":"%s\\\\.claude-account2"}},'
            '{"account_id":"codex:profile-dir:0000000000000001","provider_id":"codex","display_name":"Codex 1","compact_label":"CX1","operational_state":"ENABLED","hidden":false,"provider_metadata":{"profile_locator":"%s\\\\.codex"}},'
            '{"account_id":"antigravity:windows-user:0000000000000001","provider_id":"antigravity","display_name":"Antigravity 1","compact_label":"AG1","operational_state":"ENABLED","hidden":false,"provider_metadata":{"windows_user":"%s"}},'
            '{"account_id":"claude:profile-dir:0000000000000001","provider_id":"claude","display_name":"Claude 1","compact_label":"CL1","operational_state":"ENABLED","hidden":false,"provider_metadata":{"profile_locator":"%s\\\\.claude"}}'
            ']}' % (home, home, user, home)
        )
        r1 = self.install(initial_json)
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        r2 = self.install(reordered_json)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

        expected_prefix_order = [
            "01_account-antigravity-windows-user-0000000000000002",
            "02_account-claude-profile-dir-0000000000000002",
            "03_account-codex-profile-dir-0000000000000001",
            "04_account-antigravity-windows-user-0000000000000001",
            "05_account-claude-profile-dir-0000000000000001",
        ]
        for shell_root in ("Directory", "Directory\\Background", "Drive"):
            with self.subTest(shell_root=shell_root):
                items = self.projection(shell_root)
                ordered_keys = sorted(items.keys())
                self.assertEqual(ordered_keys[:5], expected_prefix_order, ordered_keys)
                # Stale old numbering (e.g. 01_account-antigravity-windows-user-0000000000000001) is gone
                self.assertNotIn("01_account-antigravity-windows-user-0000000000000001", ordered_keys)
                for expected_key in expected_prefix_order:
                    profile_id = expected_key.split("_", 1)[1]
                    self.assertIn(" " + profile_id + " ", items[expected_key])


class SaitulsMenuReconcileTests(unittest.TestCase):
    """The shell owns the reconciliation TRIGGER, never the projection itself."""

    SOURCE = (REPO / "SAITULS.cs").read_text(encoding="utf-8")
    CODE = strip_csharp_comments(SOURCE)

    def test_the_cascade_is_reconciled_at_startup(self):
        self.assertIn("ReconcileConsolesMenuOnStartup", self.SOURCE)
        self.assertIn("QueueUserWorkItem(delegate { ReconcileConsolesMenuOnStartup(); })", self.SOURCE)
        self.assertIn("-VerifyOnly", self.SOURCE)

    def test_a_failed_menu_update_is_visible_and_repairable(self):
        self.assertIn("EXPLORER MENU UPDATE FAILED", self.SOURCE)
        self.assertIn("Repair menu", self.SOURCE)
        self.assertIn("_menuSyncFailed", self.SOURCE)

    def test_the_shell_never_reimplements_the_installer(self):
        """One definition of the cascade: the installer's own comparison."""
        for mutation in ("New-Item", "Set-ItemProperty", "reg add", "SubCommands"):
            self.assertNotIn(mutation, self.CODE)
        self.assertIn("INSTALL_CONSOLES_MENU.PS1", self.SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
