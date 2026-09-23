"""Secret-hygiene regression for SAITULS Secure Apps.

Two halves, because either one alone would be reassuring and wrong.

**Dynamic.** A canary volume secret is pushed through a complete lifecycle --
enroll, open, lock, idle expiry, crash recovery -- and then every byte the
subsystem wrote (state file, credential store, audit log, journals, anything
else under the managed root) is searched for it. Every process argument list
the subsystem would have built is searched too. Nothing may carry it.

**Static.** The dynamic half only proves that the paths the test walked are
clean. The static half reads the subsystem's own source and refuses the
shapes that make a leak possible in the paths it did not walk: a secret on a
command line, a secret in an environment variable, a secret written to a
temp file, an exception message forwarded into the audit log.

The audit logger is an ALLOWLIST, and that is the property under test: a
denylist can only remove what somebody thought of.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SUBSYSTEM = REPO / "Scripts" / "secure_apps"
if str(SUBSYSTEM) not in sys.path:
    sys.path.insert(0, str(SUBSYSTEM))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import sa_applife          # noqa: E402
import sa_audit           # noqa: E402
import sa_broker          # noqa: E402
import sa_crypto          # noqa: E402
import sa_privhelper      # noqa: E402
import sa_privtask        # noqa: E402
import sa_storage         # noqa: E402

from test_secure_apps import Harness     # noqa: E402

CANARY = "SAITULS-CANARY-VolumeSecret-8b31f2c7d9"
CANARY_PIN = "CANARY-PIN-4821"
CANARY_RECOVERY = "111111-222222-333333-444444-555555-666666-777777-888888"

PY_SOURCES = sorted(SUBSYSTEM.glob("*.py")) + sorted(SUBSYSTEM.glob("*.pyw"))
PS_SOURCES = sorted(SUBSYSTEM.glob("*.ps1"))
ALL_SOURCES = PY_SOURCES + PS_SOURCES


class AuditAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="saituls-hygiene-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = sa_audit.AuditLog(str(self.tmp / "audit.log"))

    def test_unknown_fields_are_dropped_silently(self):
        record = self.log.write("unlock", profile_id="vault", result="ok",
                                volume_secret=CANARY, pin=CANARY_PIN,
                                recovery_password=CANARY_RECOVERY,
                                hmac_secret_output=CANARY, message="boom " + CANARY)
        self.assertNotIn("volume_secret", record)
        self.assertNotIn("pin", record)
        self.assertNotIn("recovery_password", record)
        self.assertNotIn("hmac_secret_output", record)
        self.assertNotIn("message", record)
        text = json.dumps(record)
        for canary in (CANARY, CANARY_PIN, CANARY_RECOVERY):
            self.assertNotIn(canary, text)
        self.assertNotIn(CANARY, Path(self.log.path).read_text("utf-8"))

    def test_only_allowlisted_keys_are_ever_emitted(self):
        record = self.log.write("mount", profile_id="vault", pid=42,
                                mount_path=r"V:\x", auth_provider="p",
                                backend="b", container_id="c",
                                credential_id_hash="deadbeef",
                                error_category="none", detail_code="x",
                                event="manual_lock", state="MOUNTED",
                                policy_mode="default")
        self.assertTrue(set(record).issubset(set(sa_audit.ALLOWED_FIELDS)))

    def test_unknown_error_category_is_normalised(self):
        record = self.log.write("unlock", profile_id="vault", result="failed",
                                error_category="BitLocker said " + CANARY)
        self.assertEqual(record["error_category"], "internal")
        self.assertNotIn(CANARY, json.dumps(record))

    def test_unknown_result_is_normalised(self):
        record = self.log.write("unlock", profile_id="v", result=CANARY)
        self.assertEqual(record["result"], "failed")
        self.assertNotIn(CANARY, json.dumps(record))

    def test_long_values_are_truncated_not_wrapped(self):
        record = self.log.write("mount", profile_id="v", mount_path="Z" * 5000)
        self.assertLessEqual(len(record["mount_path"]), sa_audit.MAX_SCALAR_CHARS)


class SecretBufferTests(unittest.TestCase):
    def test_repr_and_str_never_show_content(self):
        buf = sa_crypto.SecretBuffer(CANARY.encode("ascii"))
        for rendering in (repr(buf), str(buf), "%s" % buf, format(buf)):
            self.assertNotIn(CANARY, rendering)
        buf.zeroize()

    def test_exception_text_from_a_zeroized_buffer_is_clean(self):
        buf = sa_crypto.SecretBuffer(CANARY.encode("ascii"))
        buf.zeroize()
        with self.assertRaises(sa_crypto.CryptoError) as ctx:
            buf.bytes()
        self.assertNotIn(CANARY, str(ctx.exception))

    def test_unwrap_error_text_is_clean(self):
        salt = sa_crypto.new_salt()
        kek = sa_crypto.derive_kek(b"k" * 32, salt, "p", "c")
        aad = sa_crypto.build_aad("p", "c", "cred")
        nonce, ct = sa_crypto.wrap_secret(kek, CANARY.encode("ascii"), aad)
        with self.assertRaises(sa_crypto.UnwrapError) as ctx:
            sa_crypto.unwrap_secret(kek, nonce, ct,
                                    sa_crypto.build_aad("q", "c", "cred"))
        self.assertNotIn(CANARY, str(ctx.exception))
        self.assertNotIn(CANARY, repr(ctx.exception))


class LifecycleLeakTests(unittest.TestCase):
    """Push a known secret through the whole machine, then hunt for it."""

    def setUp(self):
        self.original = sa_crypto.new_volume_secret
        sa_crypto.new_volume_secret = lambda length=32: sa_crypto.SecretBuffer(
            CANARY.encode("ascii"))
        self.addCleanup(setattr, sa_crypto, "new_volume_secret", self.original)
        self.h = Harness()
        self.addCleanup(self.h.close)

    def _run_lifecycle(self):
        h = self.h
        h.backend.create = self._recovery_leaking_create(h.backend.create)
        h.enroll()
        h.broker.open("vault")
        h.broker.note_activity("vault")
        pid = h.broker.status("vault")["app_pid"]
        h.adapter.exit_tree(pid)
        h.broker.tick()
        h.broker.open("vault")
        h.broker.lock_now("vault")
        h.broker.open("vault")
        h.clock.advance(3600 * 7)
        h.broker.tick()
        h.broker.reconcile("vault")
        h.broker.shutdown()

    @staticmethod
    def _recovery_leaking_create(original):
        def create(profile, secret):
            result = original(profile, secret)
            result["recovery_password"] = CANARY_RECOVERY
            return result
        return create

    def test_no_canary_anywhere_under_the_managed_root(self):
        self._run_lifecycle()
        offenders = []
        for path in Path(self.h.root).rglob("*"):
            if not path.is_file():
                continue
            blob = path.read_bytes()
            for canary in (CANARY, CANARY_RECOVERY):
                if canary.encode("ascii") in blob:
                    offenders.append("%s carries %s" % (path, canary))
                if canary.encode("utf-16-le") in blob:
                    offenders.append("%s carries utf-16 %s" % (path, canary))
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_audit_log_records_the_lifecycle_without_the_secret(self):
        self._run_lifecycle()
        text = self.h.audit_text()
        self.assertIn("authenticate", text)
        self.assertIn("mount", text)
        self.assertIn("lock", text)
        self.assertIn("secret_zeroized", text)
        self.assertNotIn(CANARY, text)
        self.assertNotIn(CANARY_RECOVERY, text)
        for line in text.strip().splitlines():
            record = json.loads(line)
            self.assertTrue(set(record).issubset(set(sa_audit.ALLOWED_FIELDS)),
                            "audit record carries a non-allowlisted key: %s" % record)

    def test_status_payloads_carry_no_secret(self):
        self._run_lifecycle()
        blob = json.dumps(self.h.broker.status_all(), default=str)
        self.assertNotIn(CANARY, blob)
        self.assertNotIn(CANARY_RECOVERY, blob)

    def test_process_arguments_carry_no_secret(self):
        self._run_lifecycle()
        for executable, arguments, cwd in self.h.adapter.spawned:
            joined = " ".join([executable] + list(arguments) + [str(cwd)])
            self.assertNotIn(CANARY, joined)
            self.assertNotIn(CANARY_RECOVERY, joined)

    def test_environment_carries_no_secret(self):
        self._run_lifecycle()
        for key, value in os.environ.items():
            self.assertNotIn(CANARY, value, "leaked into %s" % key)
            self.assertNotIn(CANARY_RECOVERY, value, "leaked into %s" % key)


class PrivilegedHelperTransportTests(unittest.TestCase):
    """The helper pipe is the one place a secret crosses a process boundary."""

    def test_setup_elevation_command_line_carries_no_secret(self):
        """The REAL setup elevation argv, captured instead of executed.

        This is the only elevation Secure Apps ever raises, and it happens
        during Install / Repair / Remove -- never during unlock, lock or
        relock. It carries a CLI entry point and nothing else.
        """
        captured = {}
        sa_privtask.elevate_self(
            ["privileged-helper", "install", "--apply"],
            spawn=lambda argv: captured.setdefault("argv", list(argv)))
        line = " ".join(captured["argv"])
        self.assertIn("sa_cli.py", line)
        self.assertIn("privileged-helper", line)
        self.assertIn("-Verb RunAs", line)
        for word in ("secret", "passphrase", "password", "recovery", "hmac",
                     "pin'", "secret_b64"):
            self.assertNotIn(word, line.lower(),
                             "setup elevation argv mentions %r: %s" % (word, line))
        self.assertNotIn(CANARY, line)
        self.assertNotIn(CANARY_RECOVERY, line)

    def test_setup_without_a_real_interpreter_refuses_to_elevate(self):
        calls = []
        for bogus in ("", "python.exe", str(Path(tempfile.gettempdir())
                                            / "no-such-dir" / "python.exe")):
            with self.assertRaises(sa_privtask.PrivilegeTaskError):
                sa_privtask.elevate_self(["privileged-helper", "check"],
                                         python_exe=bogus,
                                         spawn=lambda argv: calls.append(argv))
        self.assertEqual(calls, [], "elevation was attempted without an interpreter")

    def test_runtime_transport_never_elevates_anything(self):
        """Unlock and lock may START the registered task -- nothing else.

        ShellExecute "runas" and Start-Process -Verb RunAs are what put a
        consent dialog on every unlock, so the runtime transport must not
        contain them at all, and must not mutate the task either.
        """
        text = (SUBSYSTEM / "sa_privhelper.py").read_text(encoding="utf-8")
        for forbidden in ("RunAs", "runas", "ShellExecute", "/Create", "/Change",
                          "/Delete"):
            self.assertNotIn(forbidden, text,
                             "the runtime transport contains %r" % forbidden)
        self.assertIn("start_task", text)

    def test_call_puts_the_secret_only_in_the_pipe_payload(self):
        written = []

        class FakePipeHelper(sa_privhelper.PrivilegedHelper):
            def _write_line(self, text):
                written.append(text)

            def _read_line(self, timeout=None):
                return json.dumps({"id": 1, "ok": True, "result": {"state": "mounted"}})

            def _drop(self):
                pass

        # verify_server=False is the documented test-only bypass: this case
        # is about the payload shape, and the identity check has its own test
        # immediately below.
        helper = FakePipeHelper(verify_server=False)
        helper._pipe = object()          # pretend the pipe is connected
        helper._verified = True
        secret = sa_crypto.SecretBuffer(CANARY.encode("ascii"))
        helper.call("unlock_mount", container="c.vhdx", mount_path="m",
                    secret_b64=sa_privhelper.encode_secret(secret))
        secret.zeroize()
        self.assertEqual(len(written), 1)
        payload = json.loads(written[0])
        self.assertEqual(payload["op"], "unlock_mount")
        # base64 of the canary, and only there
        self.assertNotIn(CANARY, written[0])
        self.assertIn("secret_b64", payload["args"])

    def test_call_refuses_before_the_helper_identity_is_verified(self):
        """No secret may cross a pipe whose peer has not been proven."""
        written = []

        class FakePipeHelper(sa_privhelper.PrivilegedHelper):
            def _write_line(self, text):
                written.append(text)

            def _read_line(self, timeout=None):
                return json.dumps({"id": 1, "ok": True, "result": {}})

        helper = FakePipeHelper()        # verification ON, as in production
        helper._pipe = object()
        secret = sa_crypto.SecretBuffer(CANARY.encode("ascii"))
        with self.assertRaises(sa_privhelper.HelperIdentityError):
            helper.call("unlock_mount", container="c.vhdx", mount_path="m",
                        secret_b64=sa_privhelper.encode_secret(secret))
        secret.zeroize()
        self.assertEqual(written, [], "a secret was written before verification")

    def test_encode_secret_is_reversible_only_by_the_helper(self):
        import base64
        secret = sa_crypto.SecretBuffer(CANARY.encode("ascii"))
        encoded = sa_privhelper.encode_secret(secret)
        secret.zeroize()
        self.assertNotIn(CANARY, encoded)
        self.assertEqual(base64.b64decode(encoded).decode("ascii"), CANARY)


#: Loads the helper's own function definitions out of its AST -- the script
#: body itself would open a pipe -- shadows Get-Command so any PATH lookup is
#: recorded, and reports what each frozen-worker launch shape produced.
WORKER_LAUNCH_HARNESS = r'''
param([string]$Helper, [string]$RuntimeRoot, [string]$Scratch)
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Helper, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw "helper does not parse: $($parseErrors[0].Message)" }
$wanted = @('Test-AbsoluteLocalPath', 'New-WorkerLaunchRefusal', 'Resolve-FidoWorkerLaunch',
            'Invoke-FidoWorker', 'Get-ErrorCategory')
$found = @()
foreach ($def in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)) {
    if ($wanted -contains $def.Name) { . ([scriptblock]::Create($def.Extent.Text)); $found += $def.Name }
}
$script:lookups = New-Object System.Collections.ArrayList
function Get-Command {
    param([Parameter(ValueFromRemainingArguments = $true)]$Rest)
    [void]$script:lookups.Add(($Rest -join ' '))
    return [pscustomobject]@{ Source = 'python'; Path = 'python' }
}
$ERR_INTERNAL = 'internal'
$FIDO_BUNDLE_DIR_NAME = 'fido-worker'
$FIDO_WORKER_EXE_NAME = 'sa_fido_worker.exe'
function Probe([string]$Worker, [string]$Root) {
    try {
        $r = Resolve-FidoWorkerLaunch -WorkerExe $Worker -RuntimeRoot $Root
        return "ok|$($r.Worker)"
    } catch { return "refused|$($_.Exception.Message)" }
}
function InvokeWorker([string]$WorkerExe) {
    $script:FidoWorkerExe = $WorkerExe
    $script:RuntimeRoot = $RuntimeRoot
    try {
        $r = Invoke-FidoWorker @{ op = 'capabilities'; args = @{} }
        return "ok|" + ($r | ConvertTo-Json -Compress)
    } catch {
        return "refused|$($_.Exception.Message)|" + (Get-ErrorCategory 'internal' $_.Exception)
    }
}
$bundle = Join-Path $RuntimeRoot $FIDO_BUNDLE_DIR_NAME
$worker = Join-Path $bundle $FIDO_WORKER_EXE_NAME
$elsewhere = Join-Path $Scratch 'sa_fido_worker.exe'
$emptyRoot = Join-Path $Scratch 'empty-root'
$r = [ordered]@{}
$r.functions = ($found | Sort-Object) -join ','
$r.worker_missing = Probe '' $RuntimeRoot
$r.worker_relative = Probe 'sa_fido_worker.exe' $RuntimeRoot
$r.worker_drive_relative = Probe 'C:sa_fido_worker.exe' $RuntimeRoot
$r.worker_unc = Probe '\\localhost\c$\sa_fido_worker.exe' $RuntimeRoot
$r.worker_not_an_exe = Probe (Join-Path $bundle 'sa_fido_worker.py') $RuntimeRoot
$r.worker_elsewhere = Probe $elsewhere $RuntimeRoot
$r.worker_wrong_name = Probe (Join-Path $bundle 'evil.exe') $RuntimeRoot
$r.worker_outside_bundle = Probe (Join-Path $RuntimeRoot $FIDO_WORKER_EXE_NAME) $RuntimeRoot
$r.worker_without_root = Probe $worker ''
$r.worker_expected_but_absent = Probe (Join-Path (Join-Path $emptyRoot $FIDO_BUNDLE_DIR_NAME) $FIDO_WORKER_EXE_NAME) $emptyRoot
$r.valid = Probe $worker $RuntimeRoot
$r.invoke_without_worker = InvokeWorker ''
$r.invoke_with_bare_name = InvokeWorker 'sa_fido_worker.exe'
$r.path_lookups = $script:lookups.Count
$r | ConvertTo-Json -Compress
'''


@unittest.skipUnless(os.name == "nt", "the helper runs under Windows PowerShell")
class ElevatedWorkerLaunchTests(unittest.TestCase):
    """The elevated helper launches only the frozen worker inside its runtime.

    There is no interpreter on the elevated runtime path any more: the helper
    starts ``sa_fido_worker.exe`` by absolute path from the protected runtime
    directory, so a worker that is relative, outside the root, wrongly named or
    absent is a refusal, never a search -- and no ``Get-Command python`` / PATH
    lookup ever happens.
    """

    HELPER = SUBSYSTEM / "sa_storage_helper.ps1"

    def _run_harness(self, helper=None):
        scratch = Path(tempfile.mkdtemp(prefix="saituls-worker-launch-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        (scratch / "empty-root").mkdir()
        runtime = scratch / "privileged"
        runtime.mkdir()
        # A stub frozen worker inside the onedir BUNDLE: Resolve checks path
        # shape and existence, not content, so an empty .exe is enough to
        # exercise the accept path.
        (runtime / "fido-worker").mkdir()
        (runtime / "fido-worker" / "sa_fido_worker.exe").write_bytes(b"MZ stub")
        harness = scratch / "harness.ps1"
        harness.write_text(WORKER_LAUNCH_HARNESS, encoding="utf-8-sig")
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(harness), "-Helper", str(helper or self.HELPER),
             "-RuntimeRoot", str(runtime), "-Scratch", str(scratch)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
        text = (completed.stdout or "").strip()
        start = text.find("{")
        self.assertGreaterEqual(start, 0, "harness produced no JSON: %s %s"
                                % (text, completed.stderr))
        return json.loads(text[start:]), runtime

    def _refused(self, value, code):
        self.assertTrue(str(value).startswith("refused|"), value)
        self.assertIn("auth_unavailable: fido worker launch refused (%s)" % code, value)

    def test_every_unusable_launch_shape_is_refused_before_a_process_exists(self):
        r, runtime = self._run_harness()
        self.assertEqual(r["functions"], "Get-ErrorCategory,Invoke-FidoWorker,"
                         "New-WorkerLaunchRefusal,Resolve-FidoWorkerLaunch,"
                         "Test-AbsoluteLocalPath")
        self._refused(r["worker_missing"], "fido_worker_missing")
        for key in ("worker_relative", "worker_drive_relative", "worker_unc"):
            self._refused(r[key], "fido_worker_not_absolute")
        self._refused(r["worker_not_an_exe"], "fido_worker_not_executable")
        self._refused(r["worker_elsewhere"], "fido_worker_unexpected")
        self._refused(r["worker_wrong_name"], "fido_worker_unexpected")
        # The runtime ROOT copy is no longer the worker: only the one inside
        # the protected onedir bundle is.
        self._refused(r["worker_outside_bundle"], "fido_worker_unexpected")
        self._refused(r["worker_without_root"], "fido_worker_unexpected")
        self._refused(r["worker_expected_but_absent"], "fido_worker_not_found")
        self.assertEqual(r["valid"], "ok|%s"
                         % (runtime / "fido-worker" / "sa_fido_worker.exe"))

    def test_an_absent_or_bare_worker_is_refused_not_resolved_from_path(self):
        r, _runtime = self._run_harness()
        for key in ("invoke_without_worker", "invoke_with_bare_name"):
            value = r[key]
            self.assertTrue(value.startswith("refused|"), value)
            self.assertTrue(value.endswith("|auth_unavailable"), value)
        self._refused(r["invoke_without_worker"], "fido_worker_missing")
        self._refused(r["invoke_with_bare_name"], "fido_worker_not_absolute")
        self.assertEqual(r["path_lookups"], 0,
                         "the elevated helper looked something up on PATH")

    def test_the_helper_source_has_no_interpreter_discovery(self):
        text = self.HELPER.read_text(encoding="utf-8")
        for pattern in (r"Get-Command\s+python", r"where(\.exe)?\s+python",
                        r"\$env:PATH", r"py(\.exe)?\s+-3"):
            self.assertIsNone(re.search(pattern, text, re.IGNORECASE),
                              "helper discovers an interpreter via %r" % pattern)


class StaticSourceTests(unittest.TestCase):
    """Refuse the shapes that make a leak possible, not just the ones seen."""

    def _read(self, path):
        return path.read_text(encoding="utf-8")

    def test_sources_exist(self):
        self.assertGreaterEqual(len(PY_SOURCES), 10)
        self.assertGreaterEqual(len(PS_SOURCES), 2)

    def test_no_secret_is_placed_on_a_command_line(self):
        # A Popen/run/Start-Process argument list that mentions a secret-ish
        # name is the classic leak: command lines are world-readable.
        forbidden = re.compile(
            r"(Popen|subprocess\.run|check_output|Start-Process|ArgumentList)"
            r"[^\n]{0,400}?(secret|passphrase|recovery|password|hmac)",
            re.IGNORECASE | re.DOTALL)
        for path in ALL_SOURCES:
            text = self._read(path)
            match = forbidden.search(text)
            self.assertIsNone(match, "%s may put a secret on a command line: %r"
                              % (path.name, match.group(0)[:160] if match else ""))

    def test_no_secret_is_placed_in_an_environment_variable(self):
        patterns = [
            re.compile(r"os\.environ\[[^\]]*\]\s*=", re.IGNORECASE),
            re.compile(r"putenv\s*\(", re.IGNORECASE),
            re.compile(r"\$env:[A-Za-z_]*(secret|password|recovery|pin)",
                       re.IGNORECASE),
        ]
        for path in ALL_SOURCES:
            text = self._read(path)
            for pattern in patterns:
                self.assertIsNone(pattern.search(text),
                                  "%s writes an environment variable" % path.name)

    def test_no_secret_is_written_to_a_temp_file(self):
        for path in PY_SOURCES:
            text = self._read(path)
            for line in text.splitlines():
                lowered = line.lower()
                if "write" not in lowered:
                    continue
                if any(word in lowered for word in
                       ("secret", "passphrase", "recovery_password", "hmac_secret")):
                    # The only permitted mentions are comments and docstrings.
                    stripped = line.strip()
                    self.assertTrue(
                        stripped.startswith("#") or stripped.startswith('"')
                        or stripped.startswith("'") or "audit.write" in lowered
                        or "recovery_shown" in lowered,
                        "%s may write a secret: %r" % (path.name, stripped[:140]))

    def test_helper_never_prints_the_unlock_secret(self):
        text = self._read(SUBSYSTEM / "sa_storage_helper.ps1")
        self.assertIn("ConvertTo-Secure", text)
        self.assertIn("SecureString", text)
        # The decoded characters must be wiped after the SecureString is built.
        self.assertIn("[char]0", text)
        for bad in ("Write-Host $secure", "Write-Output $secure",
                    "Write-Host $Base64", "-Password $Args",
                    "Write-Host $($Args.secret_b64)"):
            self.assertNotIn(bad, text, "helper leaks via %r" % bad)
        # The elevated helper READS no environment block at all -- nothing a
        # caller exports may decide where it reads its pin or what it executes;
        # every machine location comes from the known-folder API instead. The
        # single exception is a WRITE, and it is a hardening control rather
        # than a dependency: $env:PSModulePath is OVERWRITTEN with trusted
        # machine locations before any Storage or BitLocker command can
        # auto-load from a CurrentUser module directory.
        code = chr(10).join(line for line in text.splitlines()
                          if not line.lstrip().startswith("#"))
        environment_uses = re.findall(r"\$env:[A-Za-z_]+", code)
        self.assertEqual(environment_uses, ["$env:PSModulePath"],
                         "the helper touches the environment block more than "
                         "the one documented PSModulePath write: %s"
                         % environment_uses)
        self.assertIn("$env:PSModulePath = $script:TrustedModulePath", code)
        # Every external executable the high-integrity helper launches is
        # resolved to an absolute, verified %SystemRoot%\System32 path. A bare
        # name would be a PATH lookup, and PATH is assembled from variables a
        # medium-integrity account can set.
        for launcher in ("Start-Process", "Invoke-Expression", "iex "):
            self.assertNotIn(launcher, code,
                             "the helper launches through %r" % launcher)
        bare = re.findall(r"&\s+([A-Za-z][\w.-]*\.exe)", code)
        self.assertEqual(bare, [], "the helper launches %s by bare name" % bare)
        for pinned in ("diskpart.exe", "icacls.exe"):
            self.assertIn("Get-TrustedSystemExecutable '%s'" % pinned, code)
        self.assertIn("Join-Path (Get-WindowsRoot) ('System32" + chr(92) + "' + $Name)",
                      code)
        # ...and the trusted module environment is established before the
        # production loop can auto-load a Storage or BitLocker command.
        self.assertIn("Initialize-TrustedModuleEnvironment", code)
        for qualified in ("Storage\\Get-Disk", "Storage\\Mount-DiskImage",
                          "Storage\\Dismount-DiskImage", "BitLocker\\Unlock-BitLocker",
                          "BitLocker\\Lock-BitLocker", "BitLocker\\Enable-BitLocker",
                          "BitLocker\\Get-BitLockerVolume"):
            self.assertIn(qualified, code, "unqualified %s" % qualified)
        # ...and no privileged command material is written to user TEMP.
        self.assertNotIn("GetTempPath", code)
        self.assertIn("New-PrivilegedScratchDir", code)
        self.assertIn("Assert-PrivilegedScratchFile", code)
        # Exception text is mapped to a fixed category, never forwarded.
        self.assertIn("Get-ErrorCategory", text)
        self.assertNotIn("$_.Exception.Message)\" } | ConvertTo-Json", text)

    def test_helper_error_replies_carry_no_exception_text(self):
        text = self._read(SUBSYSTEM / "sa_storage_helper.ps1")
        reply = re.search(r"\$writer\.WriteLine\(\(@\{ id = \$id; ok = \$false;.*?\)\)",
                          text, re.DOTALL)
        self.assertIsNotNone(reply, "error reply block not found")
        self.assertNotIn("Exception.Message", reply.group(0))

    def test_audit_module_is_an_allowlist_not_a_denylist(self):
        text = self._read(SUBSYSTEM / "sa_audit.py")
        self.assertIn("ALLOWED_FIELDS", text)
        self.assertIn("if key not in ALLOWED_FIELDS", text)
        self.assertNotIn("REDACT_PATTERNS", text)

    def test_broker_logs_categories_not_exception_text(self):
        text = self._read(SUBSYSTEM / "sa_broker.py")
        self.assertIsNone(re.search(r"audit\.write\([^)]*str\(exc\)", text))
        self.assertIsNone(re.search(r"_log\([^)]*str\(exc\)", text))

    def test_credential_store_holds_ciphertext_only(self):
        text = self._read(SUBSYSTEM / "sa_auth.py")
        self.assertIn('"ciphertext": b64e(self.ciphertext)', text)
        # No plaintext secret field may be serialised.
        for bad in ('"volume_secret"', '"plaintext"', '"recovery_password"',
                    '"hmac_secret_output"'):
            self.assertNotIn(bad, text)

    def test_state_module_declares_no_secret_field(self):
        import sa_state
        for name in sa_state.ENTRY_FIELDS:
            self.assertNotIn("secret", name)
            self.assertNotIn("password", name)
            self.assertNotIn("key", name)

    def test_gui_never_persists_recovery_material(self):
        text = self._read(SUBSYSTEM / "secure_apps_gui.py")
        self.assertIn("RecoveryDialog", text)
        self.assertIn("setReadOnly(True)", text)
        for bad in ("write_text", "open(", "json.dump"):
            self.assertNotIn("recovery" + bad, text)
        # The dialog must not offer to save the value anywhere.
        self.assertNotIn("QFileDialog", text)

    def test_cli_does_not_accept_a_secret_argument(self):
        text = self._read(SUBSYSTEM / "sa_cli.py")
        for bad in ("--password", "--secret", "--passphrase", "--recovery"):
            self.assertNotIn(bad, text)


class WorkflowHygieneTests(unittest.TestCase):
    """Hygiene regressions for CI workflows."""

    def test_workflow_command_lines_contain_no_tabs(self):
        workflow_dir = REPO / ".github" / "workflows"
        workflows = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
        self.assertGreaterEqual(len(workflows), 1, "workflow files must exist")
        offenders = []
        for wf in workflows:
            lines = wf.read_text(encoding="utf-8").splitlines()
            in_run = False
            run_indent = 0
            for idx, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("run:"):
                    in_run = True
                    run_indent = len(line) - len(line.lstrip(" "))
                    if "\t" in line:
                        offenders.append("%s:%d: tab in run line: %r" % (wf.name, idx, line))
                    continue
                if in_run:
                    current_indent = len(line) - len(line.lstrip(" ")) if stripped else run_indent + 1
                    if stripped and current_indent <= run_indent and (stripped.startswith("-") or ":" in stripped):
                        in_run = False
                    elif "\t" in line:
                        if not stripped.startswith("#"):
                            offenders.append("%s:%d: tab in executable command line: %r" % (wf.name, idx, line))
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main(verbosity=2)
