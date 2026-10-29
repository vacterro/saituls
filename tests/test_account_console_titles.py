"""Real hidden console title churn; no provider login, network or registry writes."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = Path(os.environ.get("SAI_TITLE_TEST_ENGINE", ROOT / "Scripts/accounts/bin/sai-accounts.exe"))


def finish_owned_console(process):
    """Wait for this fixture's guard, including when another assertion failed."""
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class AccountConsoleTitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="sai-console-title-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.folder = Path(cls.temp.name)
        cls.vendor = cls.folder / "vendor-title.exe"
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        subprocess.run([str(compiler), "-nologo", "-out:" + str(cls.vendor), "-r:System.dll",
                        "-r:System.Web.Extensions.dll", str(ROOT / "tests/console_title_churn.cs")], check=True)
        if not ENGINE.is_file():
            raise RuntimeError("Build the real account engine before the title contract test")

    def launch(self, ordinal, env_overrides=None, engine_path=None):
        title = "[CL%d] Claude %d - owned console contract" % (ordinal, ordinal)
        account = "claude:profile-dir:%016x" % ordinal
        result = self.folder / ("result-%d.json" % ordinal)
        if result.exists():
            result.unlink()
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen([
            str(engine_path or ENGINE), "console-title-guard", "--title64", base64.b64encode(title.encode()).decode(),
            "--account", account, "--", str(self.vendor), title, account, str(result)
        ], startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE,
            env=dict(os.environ, **(env_overrides or {})))
        # This Popen handle identifies only this test's newly created guard.
        self.addCleanup(finish_owned_console, process)
        return process, result, title, account

    def check_result(self, launched):
        process, result, title, account = launched
        code = process.wait(timeout=20)
        self.assertTrue(result.is_file(), "The real vendor fixture did not execute")
        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["samples"]), 20)
        self.assertEqual(payload["expected_account"], account)
        bad = [s for s in payload["samples"] if not s["restored"]]
        self.assertEqual(bad, [], "Account title/property must recover within 150ms: %r" % bad)
        self.assertEqual(code, 0)
        self.assertEqual(payload["expected_title"], title)
        print("TITLE_SAMPLES=%d max_restore_ms=%d account=%s" % (
            len(payload["samples"]), max(s["latency_ms"] for s in payload["samples"]), account))

    def test_vendor_response_keeps_the_exact_account_window_identity(self):
        self.check_result(self.launch(1))

    def test_two_concurrent_accounts_keep_separate_window_identities(self):
        first, second = self.launch(1), self.launch(2)
        self.check_result(first)
        self.check_result(second)

    def launch_antigravity(self, compact, workdir, exit_code=0, ctrl_c=False, env_overrides=None):
        """Invoke the real fixed Antigravity constructor, not a parallel launcher."""
        bindir = self.folder / "bin"
        bindir.mkdir(exist_ok=True)
        if not (bindir / "agy.exe").exists():
            shutil.copyfile(self.vendor, bindir / "agy.exe")
        account = "antigravity:windows-user:" + ("7" if compact == "AG7" else "8") * 16
        title = "[%s] Antigravity - %s" % (compact, workdir.name or str(workdir))
        result = self.folder / ("antigravity-%s.json" % compact)
        env = dict(os.environ, SAI_ACCOUNTS_ROOT=str(self.folder),
                   SAI_TITLE_FIXTURE_TITLE=title, SAI_TITLE_FIXTURE_ACCOUNT=account,
                   SAI_TITLE_FIXTURE_RESULT=str(result), SAI_TITLE_FIXTURE_EXIT=str(exit_code),
                   SAI_TITLE_FIXTURE_CTRL_C="1" if ctrl_c else "0")
        env.update(env_overrides or {})

        def ps_quote(value):
            return "'" + str(value).replace("'", "''") + "'"

        # The same oracle supports the saved pre-fix constructor (one parameter)
        # and the repaired constructor; before the fix the real vendor is bare.
        script = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "$a=[Reflection.Assembly]::LoadFile(%s);"
            "$m=$a.GetType('SaiAccounts').GetMethod('AntigravityLaunch',"
            "[Reflection.BindingFlags]'NonPublic,Static');"
            "$p=if($m.GetParameters().Length -eq 1){$m.Invoke($null,@(%s))}"
            "else{$m.Invoke($null,@(%s,%s,%s))};"
            "@{image=$p.FileName;arguments=$p.Arguments;cwd=$p.WorkingDirectory}|ConvertTo-Json -Compress"
        ) % (ps_quote(ENGINE.resolve()), ps_quote(workdir), ps_quote(workdir),
             ps_quote(account), ps_quote(compact))
        built = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                               capture_output=True, text=True, encoding="utf-8", check=True, env=env)
        info = json.loads(built.stdout.strip())
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen(subprocess.list2cmdline([info["image"]]) + " " + info["arguments"],
                                   cwd=info["cwd"], env=env, startupinfo=startup,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.addCleanup(finish_owned_console, process)
        return process, result, title, account, exit_code, str(workdir)

    def check_antigravity_result(self, launched):
        process, result, title, account, exit_code, workdir = launched
        actual_code = process.wait(timeout=20)
        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(payload["failures"], 0, "the vendor must retain the canonical title")
        self.assertEqual(actual_code, exit_code)
        self.assertEqual(payload["expected_title"], title)
        self.assertEqual(payload["expected_account"], account)
        self.assertEqual(payload["arguments"], ["--dangerously-skip-permissions"])
        self.assertEqual(os.path.normcase(os.path.realpath(payload["cwd"])),
                         os.path.normcase(os.path.realpath(workdir)))
        self.assertEqual(payload["console_process_count"], 2, "guard and vendor must share one console")
        self.assertEqual(payload["console_process_count_after"], 2, "the guard lives until its vendor exits")
        self.assertTrue(payload["sid"].startswith("S-1-5-21-"))

    def test_antigravity_constructor_keeps_title_unicode_workdir_and_nonzero_exit(self):
        workdir = self.folder / "Project spaces üõ"
        workdir.mkdir(exist_ok=True)
        self.check_antigravity_result(self.launch_antigravity("AG7", workdir, exit_code=7))

    def test_concurrent_antigravity_constructors_have_distinct_titles_at_drive_root(self):
        root = Path(self.folder.anchor)
        first = self.launch_antigravity("AG7", root)
        second = self.launch_antigravity("AG8", root)
        self.check_antigravity_result(first)
        self.check_antigravity_result(second)

    def test_antigravity_guard_preserves_vendor_ctrl_c_and_input_mode(self):
        launched = self.launch_antigravity("AG7", self.folder, ctrl_c=True)
        self.check_antigravity_result(launched)
        payload = json.loads(launched[1].read_text(encoding="utf-8"))
        self.assertEqual(payload["ctrl_c_received"], 1)
        self.assertTrue(payload["input_readable"])
        self.assertEqual(payload["input_mode_before"], payload["input_mode_after"])

    def test_antigravity_interactive_child_drops_capture_only_color_hints(self):
        hints = {"NO_COLOR": "1", "TERM": "dumb", "COLORTERM": "truecolor", "SAI_TITLE_FIXTURE_PRESERVE": "kept"}
        launched = self.launch_antigravity("AG7", self.folder, env_overrides=hints)
        self.check_antigravity_result(launched)
        payload = json.loads(launched[1].read_text(encoding="utf-8"))
        self.assertIsNone(payload["no_color"], "capture-only NO_COLOR must not disable interactive vendor color")
        self.assertIsNone(payload["term"], "capture-only TERM=dumb must not disable interactive vendor color")
        self.assertEqual(payload["colorterm"], "truecolor")
        self.assertEqual(payload["preserved_marker"], "kept")

    def test_generic_title_guard_keeps_its_original_environment(self):
        launched = self.launch(9, {"NO_COLOR": "1", "TERM": "dumb", "SAI_TITLE_FIXTURE_PRESERVE": "kept"})
        self.check_result(launched)
        payload = json.loads(launched[1].read_text(encoding="utf-8"))
        self.assertEqual(payload["no_color"], "1")
        self.assertEqual(payload["term"], "dumb")
        self.assertEqual(payload["preserved_marker"], "kept")

    @unittest.skipUnless(os.environ.get("SAI_NATIVE_INPUT_ACCEPTANCE") == "1",
                         "Explicit Windows desktop acceptance: SAI_NATIVE_INPUT_ACCEPTANCE=1")
    def test_antigravity_native_input_and_selected_workspace_trust(self):
        controller = self.folder / "console-input-test.exe"
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        subprocess.run([str(compiler), "-nologo", "-out:" + str(controller),
                        "-r:System.dll", "-r:System.Web.Extensions.dll", "-r:System.Windows.Forms.dll",
                        str(ROOT / "tests/account_console_input_test.cs")], check=True)
        folder = self.folder / "input-fixtures"
        run = subprocess.run([str(controller), str(ENGINE.resolve()), str(folder)],
                             capture_output=True, text=True, encoding="utf-8", timeout=40)
        self.assertTrue(run.stdout.strip(), "Native keyboard instrument must report a real result")
        payload = json.loads(run.stdout.strip())
        self.assertNotIn("instrument_error", payload, str(payload))
        self.assertEqual(payload["known_good_native_control"], "PASS", str(payload))
        self.assertEqual(run.returncode, 0, str(payload))
        self.assertEqual(payload["failures"], 0, str(payload))
        self.assertTrue(payload["owned_fixtures_exited"], str(payload))
        self.assertTrue(payload["clipboard_restored"], str(payload))
        print("NATIVE_INPUT=" + json.dumps(payload, ensure_ascii=True, sort_keys=True))

    def test_antigravity_selected_workspace_trust_preserves_other_settings(self):
        controller = self.folder / "console-trust-test.exe"
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        subprocess.run([str(compiler), "-nologo", "-out:" + str(controller),
                        "-r:System.dll", "-r:System.Web.Extensions.dll", "-r:System.Windows.Forms.dll",
                        str(ROOT / "tests/account_console_input_test.cs")], check=True)
        run = subprocess.run([str(controller), "--trust-only", str(ENGINE.resolve()),
                              str(self.folder / "trust-only")],
                             capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertTrue(run.stdout.strip(), run.stderr)
        payload = json.loads(run.stdout.strip())
        self.assertEqual(run.returncode, 0, str(payload))
        self.assertEqual(payload["failures"], 0, str(payload))
        self.assertEqual(len(payload["checks"]), 11, str(payload))
        print("WORKSPACE_TRUST=" + json.dumps(payload, ensure_ascii=True, sort_keys=True))

    def test_running_title_guard_can_be_replaced_without_restarting_its_vendor(self):
        target_dir = self.folder / "owned-image-replacement"
        target_dir.mkdir()
        target = target_dir / "sai-accounts.exe"
        shutil.copyfile(ENGINE, target)
        # A valid PE overlay makes this a distinct old image, forcing replacement.
        with target.open("ab") as f:
            f.write(b"owned previous-image fixture overlay")
        ready, release = target_dir / "ready", target_dir / "release"
        launched = self.launch(10, {"SAI_TITLE_FIXTURE_READY": str(ready),
                                    "SAI_TITLE_FIXTURE_RELEASE": str(release)}, target)
        process = launched[0]
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "the owned old guard/vendor must be running before replacement")
            self.assertIsNone(process.poll())
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            script = "$ErrorActionPreference='Stop'; . %s; Install-SaiExecutableImage -Source %s -Target %s -AllowedDirectory %s | ConvertTo-Json -Compress" % (
                quote(ROOT / "Scripts/accounts/InstallImage.ps1"), quote(ENGINE.resolve()), quote(target), quote(target_dir))
            result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(result.stdout.strip(), result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["Status"], "applied")
            self.assertTrue(Path(payload["RetainedImage"]).is_file())
            self.assertEqual(target.read_bytes(), ENGINE.read_bytes())
            self.assertIsNone(process.poll(), "replacement must keep the old guard/vendor alive")
            # Idempotence must not rotate or remove the retained image again.
            second = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                                    capture_output=True, text=True, encoding="utf-8", check=True)
            self.assertEqual(json.loads(second.stdout)["Status"], "already_correct")
            self.assertTrue(Path(payload["RetainedImage"]).is_file())
        finally:
            release.write_text("release", encoding="ascii")
            finish_owned_console(process)
        self.check_result(launched)


if __name__ == "__main__":
    unittest.main(verbosity=2)
