"""Explorer's bounded, typed workdir repair contract (no real ACL mutation)."""
import json
import os
import shutil
import tempfile
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = ROOT / "Scripts" / "consoles" / "ACCOUNTS.ps1"
CONSOLES = ROOT / "Scripts" / "consoles" / "CONSOLES.ps1"
ACCOUNT = "antigravity:windows-user:407fdf8621212585"


def quoted(value):
    return "'" + value.replace("'", "''") + "'"


class ExplorerWorkdirRepairTests(unittest.TestCase):
    def run_repair(self, launches, grant=None, path=r"V:\___VAC\__P\_EVERYTHING", account=ACCOUNT):
        plan = quoted(json.dumps({"launches": launches, "grant": grant}, ensure_ascii=True))
        script = r'''
[Console]::OutputEncoding=[Text.Encoding]::UTF8
. %s
$script:plan = %s | ConvertFrom-Json
$script:calls = @()
$script:index = 0
function Invoke-AccountsEngineLaunch {
    param($Engine, $Arguments, $WorkDir)
    $script:calls += [pscustomobject]@{ op = 'launch'; account = $Arguments[1]; path = $Arguments[3]; engine = $Engine }
    $row = $script:plan.launches[$script:index]
    $script:index++
    [pscustomobject]@{
        Schema = 'sai.accounts/launch/1'; Status = [string]$row.status; EngineReason = [string]$row.reason
        Ok = ([string]$row.status -eq 'launched'); Category = [string]$row.status
        Reason = [string]$row.status; ExitCode = 0; Pid = 123
    }
}
function Invoke-AccountsEngineGrantWorkdir {
    param($Engine, $AccountId, $WorkDir)
    $script:calls += [pscustomobject]@{ op = 'grant-workdir'; account = $AccountId; path = $WorkDir; engine = $Engine }
    $status = [string]$script:plan.grant
    [pscustomobject]@{
        Schema = 'sai.accounts/grant-workdir/1'; Status = $status; EngineReason = $null
        Ok = ($status -in @('granted', 'already_granted')); Category = $status
        Reason = $status; ExitCode = 0; Pid = 0
    }
}
$r = Invoke-AccountsEngineLaunchWithWorkdirRepair -Engine 'same-engine.exe' -AccountId %s -WorkDir (%s | ConvertFrom-Json)
[pscustomobject]@{ verdict = $r.Verdict; calls = @($script:calls) } | ConvertTo-Json -Compress -Depth 6
''' % (quoted(str(ACCOUNTS)), plan, quoted(account), quoted(json.dumps(path, ensure_ascii=True)))
        process = subprocess.run(
            ["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("{", process.stdout, process.stdout + process.stderr)
        return json.loads(process.stdout[process.stdout.find("{"):])

    def assert_calls(self, result, operations, account=ACCOUNT, path=r"V:\___VAC\__P\_EVERYTHING"):
        self.assertEqual([row["op"] for row in result["calls"]], operations)
        for row in result["calls"]:
            self.assertEqual((row["account"], row["path"], row["engine"]),
                             (account, path, "same-engine.exe"))

    def test_granted_and_already_granted_retry_once(self):
        for grant in ("granted", "already_granted"):
            with self.subTest(grant=grant):
                path = r"V:\___VAC\__P\My Big Project"
                result = self.run_repair([{"status": "WORKDIR_UNAVAILABLE", "reason": "ACCESS_DENIED"},
                                          {"status": "launched"}], grant, path)
                self.assertTrue(result["verdict"]["Ok"], result)
                self.assert_calls(result, ["launch", "grant-workdir", "launch"], path=path)

    def test_unicode_and_drive_root_keep_exact_path(self):
        for path in (r"V:\___VAC\__P\Проект Õun", "V:\\"):
            with self.subTest(path=path):
                result = self.run_repair([{"status": "WORKDIR_UNAVAILABLE", "reason": "ACCESS_DENIED"},
                                          {"status": "launched"}], "granted", path)
                self.assertTrue(result["verdict"]["Ok"], result)
                self.assert_calls(result, ["launch", "grant-workdir", "launch"], path=path)

    def test_every_non_permission_refusal_skips_grant(self):
        for status, reason in (("WORKDIR_UNAVAILABLE", "NOT_FOUND"),
                               ("WORKDIR_UNAVAILABLE", "NOT_READABLE"),
                               ("LIFECYCLE_FORBIDDEN", None), ("ACCOUNT_NOT_FOUND", None),
                               ("AUTH_REQUIRED", None), ("CONTEXT_OFFLINE", None),
                               ("launch_failed", "ACCESS_DENIED")):
            with self.subTest(status=status, reason=reason):
                result = self.run_repair([{"status": status, "reason": reason}])
                self.assert_calls(result, ["launch"])
                self.assertEqual(result["verdict"]["Status"], status)

    def test_grant_failures_remain_typed_without_retry(self):
        for status in ("ACL_READ_FAILED", "ACL_MUTATION_FAILED", "EFFECTIVE_ACCESS_DENIED",
                       "WORKDIR_REPARSE_REFUSED", "WORKDIR_TARGET_REFUSED", "WORKDIR_NOT_FOUND",
                       "WORKDIR_NOT_READABLE", "HELPER_UNAVAILABLE", "LIFECYCLE_FORBIDDEN",
                       "BACKEND_UNSUPPORTED", "CONTEXT_IDENTITY_MISSING"):
            with self.subTest(status=status):
                result = self.run_repair([{"status": "WORKDIR_UNAVAILABLE", "reason": "ACCESS_DENIED"}], status)
                self.assert_calls(result, ["launch", "grant-workdir"])
                self.assertEqual(result["verdict"]["Status"], status)
                self.assertFalse(result["verdict"]["Ok"])

    def test_accepted_but_ineffective_grant_stops(self):
        result = self.run_repair([{"status": "WORKDIR_UNAVAILABLE", "reason": "ACCESS_DENIED"},
                                  {"status": "WORKDIR_UNAVAILABLE", "reason": "ACCESS_DENIED"}], "granted")
        self.assert_calls(result, ["launch", "grant-workdir", "launch"])
        self.assertEqual(result["verdict"]["Category"], "WORKDIR_GRANT_INEFFECTIVE")
        self.assertIn("still cannot enter", result["verdict"]["Reason"])

    def test_account_cannot_change_during_grant_or_retry(self):
        other = "antigravity:windows-user:other-id"
        result = self.run_repair([{"status": "WORKDIR_UNAVAILABLE", "reason": "ACCESS_DENIED"},
                                  {"status": "launched"}], "granted", account=other)
        self.assert_calls(result, ["launch", "grant-workdir", "launch"], account=other)

    def test_prose_and_non_launch_envelope_do_not_provision(self):
        text = ACCOUNTS.read_text(encoding="utf-8")
        self.assertIn("$initial.EngineReason -ne 'ACCESS_DENIED'", text)
        self.assertIn("$initial.Schema -ne 'sai.accounts/launch/1'", text)
        self.assertNotIn("auth ", text[text.index("function Invoke-AccountsEngineLaunchWithWorkdirRepair"):])
        self.assertIn("Invoke-AccountsEngineLaunchWithWorkdirRepair", CONSOLES.read_text(encoding="utf-8"))
        result = self.run_repair([{"status": "launch_failed", "reason": None}])
        self.assert_calls(result, ["launch"])


class ExplorerBoundaryRepairTests(unittest.TestCase):
    """Real CONSOLES.ps1 boundary, real child argv, simulated engine envelopes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="saituls-repair-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        source = Path(self.tmp) / "engine.cs"
        self.engine = str(Path(self.tmp) / "engine.exe")
        self.history = Path(self.tmp) / "history.txt"
        self.sequence = Path(self.tmp) / "sequence.txt"
        self.log = Path(self.tmp) / "console.log"
        source.write_text(r'''
using System;
using System.IO;
using System.Text;
class Engine {
    static int Main(string[] args) {
        string history = Environment.GetEnvironmentVariable("STUB_HISTORY");
        string[] responses = File.ReadAllLines(Environment.GetEnvironmentVariable("STUB_SEQUENCE"));
        int index = File.Exists(history) ? File.ReadAllLines(history).Length : 0;
        File.AppendAllText(history, string.Join("\t", args) + "\n", Encoding.UTF8);
        Console.OutputEncoding = Encoding.UTF8;
        string noise = Environment.GetEnvironmentVariable("STUB_NOISE");
        if (noise != null) Console.WriteLine(noise);
        Console.WriteLine(responses[index]);
        return args[0] == "list" || responses[index].Contains("\"status\":\"launched\"") ||
            responses[index].Contains("\"status\":\"granted\"") ||
            responses[index].Contains("\"status\":\"already_granted\"") ? 0 : 1;
    }
}
''', encoding="utf-8")
        compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
        if not compiler.exists():
            compiler = Path(str(compiler).replace("Framework64", "Framework"))
        built = subprocess.run([str(compiler), "-nologo", "-out:" + self.engine, str(source)],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)

    def run_boundary(self, statuses, path, noise=None):
        list_response = {"schema": "sai.accounts/list/1", "accounts": [{
            "account_id": ACCOUNT, "provider_id": "antigravity", "display_name": "AG2",
            "operational_state": "ENABLED", "hidden": False,
            "provider_metadata": {"windows_user": "saituls-test-managed-user"}}]}
        lines = [list_response]
        for status, reason in statuses:
            action = "grant" if status in ("granted", "already_granted", "ACL_MUTATION_FAILED",
                                           "EFFECTIVE_ACCESS_DENIED") else "launch"
            item = {"schema": "sai.accounts/grant-workdir/1" if action == "grant" else "sai.accounts/launch/1",
                    "status": status, "account_id": ACCOUNT, "workdir": path}
            if reason: item["reason"] = reason
            lines.append(item)
        self.sequence.write_text("\n".join(json.dumps(line, ensure_ascii=True, separators=(",", ":"))
                                            for line in lines), encoding="utf-8")
        env = dict(os.environ, SAI_ACCOUNTS_EXE=self.engine, STUB_HISTORY=str(self.history),
                   STUB_SEQUENCE=str(self.sequence), SAITULS_CONSOLE_LOG=str(self.log))
        if noise is not None: env["STUB_NOISE"] = noise
        profile = "account-" + ACCOUNT.replace(":", "-")
        process = subprocess.run(["powershell", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                  "-File", str(CONSOLES), "-FromExplorer", "-NoErrorDialog",
                                  "-Profile", profile, "-WorkDir", path, "-Federation", "on"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 cwd=str(ROOT), env=env, timeout=90)
        history = self.history.read_text(encoding="utf-8-sig").splitlines()
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8-sig").splitlines()]
        return process, history, rows

    def test_explorer_repairs_once_and_logs_one_success(self):
        path = str(Path(self.tmp) / "Проект Õun With Spaces")
        Path(path).mkdir()
        process, history, rows = self.run_boundary([
            ("WORKDIR_UNAVAILABLE", "ACCESS_DENIED"), ("granted", None), ("launched", None)], path)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(history, ["list\t--all",
                                   "launch\t" + ACCOUNT + "\t--workdir\t" + path,
                                   "grant-workdir\t" + ACCOUNT + "\t--workdir\t" + path,
                                   "launch\t" + ACCOUNT + "\t--workdir\t" + path])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "launched")
        self.assertEqual(rows[0]["initial_launch"], {"reason": "ACCESS_DENIED", "status": "WORKDIR_UNAVAILABLE"})
        self.assertEqual(rows[0]["grant"], {"status": "granted"})
        self.assertEqual(rows[0]["retry"]["status"], "launched")

    def test_explorer_preserves_typed_grant_failure(self):
        path = str(Path(self.tmp) / "Project")
        Path(path).mkdir()
        process, history, rows = self.run_boundary([
            ("WORKDIR_UNAVAILABLE", "ACCESS_DENIED"), ("ACL_MUTATION_FAILED", None)], path)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(len(history), 3)
        self.assertEqual(rows[0]["outcome"], "failed")
        self.assertEqual(rows[0]["category"], "ACL_MUTATION_FAILED")
        self.assertEqual(rows[0]["grant"], {"status": "ACL_MUTATION_FAILED"})
        self.assertIsNone(rows[0]["retry"])

    def test_explorer_unstructured_access_denied_never_mutates_acl(self):
        path = str(Path(self.tmp) / "Project")
        Path(path).mkdir()
        process, history, rows = self.run_boundary(
            [("WORKDIR_UNAVAILABLE", None)], path, noise="ACCESS_DENIED from arbitrary stdout")
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(len(history), 2)
        self.assertIsNone(rows[0]["grant"])

    def test_explorer_not_found_never_mutates_acl(self):
        path = str(Path(self.tmp) / "Project")
        Path(path).mkdir()
        process, history, rows = self.run_boundary([("WORKDIR_UNAVAILABLE", "NOT_FOUND")], path)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(len(history), 2)
        self.assertEqual(rows[0]["initial_launch"]["reason"], "NOT_FOUND")
        self.assertIsNone(rows[0]["grant"])


if __name__ == "__main__":
    unittest.main()
