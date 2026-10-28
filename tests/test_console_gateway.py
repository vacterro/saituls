"""T-264: native Explorer gateway (SaiConsoleLauncher.exe) for the agent
console cascade.

Proves, against a disposable HKCU class root and a disposable gateway dir:
  * the registry verb is ONLY  "<gateway>" <projection id> "%V"  on all three
    shell roots (Directory, Directory\\Background, Drive);
  * the gateway is a GUI-subsystem binary (no console window of its own);
  * Explorer root matrix: ASCII, spaces, Cyrillic, Estonian and a drive root
    arrive exactly once and exactly right, through the verb Explorer would run
    (raw CreateProcess command line) and through CommandLineToArgvW for the
    hop to PowerShell;
  * failure matrix: missing root (env and moved sidecar), invalid id, unknown
    id, missing folder, inaccessible folder, missing vendor CLI -> a
    categorized durable log row, a nonzero exit, and nothing launched (P0-5:
    detected before any visible vendor session);
  * the versioned request saituls.console-launch/1 is emitted and consumed.

Every gate has a deliberate red control that must fail its own predicate.
SAITULS_TEST_REPO points the suite at another tree (used to record the
pre-fix failing count).
"""
import ctypes
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from ctypes import wintypes
from pathlib import Path

REPO = Path(os.environ.get("SAITULS_TEST_REPO") or Path(__file__).resolve().parents[1])
INSTALLER = REPO / "Installers" / "INSTALL_CONSOLES_MENU.PS1"
CONSOLES = REPO / "Scripts" / "consoles"
GATEWAY_SOURCE = CONSOLES / "SaiConsoleLauncher.cs"
ROOTS = ("Directory", "Directory\\Background", "Drive")
PS = os.path.join(os.environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe")

STATE = {}


def psraw(script):
    p = subprocess.run([PS, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    return p.returncode, p.stdout, p.stderr


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def argv(cmdline):
    fn = ctypes.windll.shell32.CommandLineToArgvW
    fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    fn.restype = ctypes.POINTER(wintypes.LPWSTR)
    n = ctypes.c_int()
    ptr = fn(cmdline, ctypes.byref(n))
    out = [ptr[i] for i in range(n.value)]
    ctypes.windll.kernel32.LocalFree(ptr)
    return out


def pe_subsystem(path):
    with open(path, "rb") as f:
        head = f.read(4096)
    off = struct.unpack_from("<I", head, 0x3C)[0]
    if head[off:off + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", head, off + 24 + 68)[0]


VERB = re.compile(r'^"(?P<exe>[^"]+\\SaiConsoleLauncher\.exe)" (?P<id>[A-Za-z0-9_-]+) "%V"$')


def setUpModule():
    tmp = tempfile.mkdtemp(prefix="saituls-gw-")
    STATE["tmp"] = tmp
    STATE["gwdir"] = os.path.join(tmp, "gateway")
    STATE["classes"] = "HKCU:\\Software\\Classes\\SaitulsGatewayTest_" + os.urandom(4).hex()
    # Fixture install root: the real launcher scripts plus a registry with two
    # deterministic profiles -- one whose CLI always exists, one whose never does.
    froot = os.path.join(tmp, "root")
    fcons = os.path.join(froot, "Scripts", "consoles")
    os.makedirs(fcons)
    for name in ("CONSOLES.ps1", "ACCOUNTS.ps1"):
        shutil.copy2(CONSOLES / name, fcons)
    doc = json.loads((CONSOLES / "consoles.json").read_text(encoding="utf-8-sig"))
    base = {"enabled": True, "arguments": ["-NoProfile"], "environment": {},
            "working_directory_mode": "ask", "requires_admin": False}
    doc["profiles"] = [dict(base, id="gw-ok", label="GW OK", command="powershell", title_suffix="GW"),
                       dict(base, id="gw-missing", label="GW Missing",
                            command="saituls-no-such-cli-7f6a", title_suffix="GW")]
    Path(fcons, "consoles.json").write_text(json.dumps(doc), encoding="utf-8")
    STATE["froot"] = froot
    # Explorer matrix folders.
    names = {"ascii": "proj", "spaces": "with some spaces", "cyrillic": "проект папка",
             "estonian": "õäöü kaust"}
    STATE["dirs"] = {}
    for k, v in names.items():
        d = os.path.join(tmp, "targets", v)
        os.makedirs(d)
        STATE["dirs"][k] = d
    STATE["install"] = subprocess.run(
        [PS, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
         "-RegistryClassesRoot", STATE["classes"], "-GatewayDir", STATE["gwdir"], "-Federation", "off"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, cwd=str(REPO))


def tearDownModule():
    psraw("if (Test-Path -LiteralPath %s) { Remove-Item -LiteralPath %s -Recurse -Force }"
          % (q(STATE["classes"]), q(STATE["classes"])))
    shutil.rmtree(STATE["tmp"], ignore_errors=True)


def projection(root):
    rc, out, _ = psraw(
        "$b = %s + '\\%s\\shell\\SaitulsAgents\\shell'; if (Test-Path -LiteralPath $b) { "
        "Get-ChildItem -LiteralPath $b | ForEach-Object { $_.PSChildName + '=' + "
        "(Get-Item -LiteralPath ($_.PSPath + '\\command')).GetValue('') } }" % (q(STATE["classes"]), root))
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def gateway_exe():
    return os.path.join(STATE["gwdir"], "SaiConsoleLauncher.exe")


class Run:
    def __init__(self, code, log, plan):
        self.code, self.log, self.plan = code, log, plan


def run_gateway(cmdline, env_extra=None, dry=True, root="fixture"):
    work = tempfile.mkdtemp(dir=STATE["tmp"], prefix="run-")
    log = os.path.join(work, "consoles-launch.log")
    plan = os.path.join(work, "plan.json")
    env = dict(os.environ)
    env.update({"SAITULS_CONSOLE_LOG": log, "SAITULS_GATEWAY_NO_DIALOG": "1"})
    env.pop("SAITULS_CONSOLE_ROOT", None)
    env.pop("SAITULS_GATEWAY_DRYRUN", None)
    if root == "fixture":
        env["SAITULS_CONSOLE_ROOT"] = STATE["froot"]
    elif root:
        env["SAITULS_CONSOLE_ROOT"] = root
    if dry:
        env["SAITULS_GATEWAY_DRYRUN"] = plan
    env.update(env_extra or {})
    # A string is handed to CreateProcess verbatim: exactly what Explorer does
    # after substituting %V into the verb.
    p = subprocess.run(cmdline, env=env, timeout=180, capture_output=True)
    gwlog = os.path.join(work, "console-gateway.log")
    rows = []
    if os.path.exists(gwlog):
        rows = [json.loads(l) for l in Path(gwlog).read_text(encoding="utf-8").splitlines() if l.strip()]
    data = json.loads(Path(plan).read_text(encoding="utf-8")) if os.path.exists(plan) else None
    return Run(p.returncode, rows, data)


def verb_for(root_name, profile_id, target):
    items = projection(root_name)
    template = sorted(items.values())[0]
    m = VERB.match(template)
    assert m, template
    return template.replace(" " + m.group("id") + " ", " " + profile_id + " ", 1).replace("%V", target)


class InstallTests(unittest.TestCase):
    def test_installer_succeeds_and_publishes_the_gateway(self):
        r = STATE["install"]
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(gateway_exe()))
        side = json.loads(Path(STATE["gwdir"], "SaiConsoleLauncher.root.json").read_text(encoding="utf-8"))
        self.assertEqual(os.path.normcase(side["root"]), os.path.normcase(str(REPO)))

    def test_verb_is_only_gateway_id_and_target_on_all_three_roots(self):
        for root in ROOTS:
            with self.subTest(root=root):
                items = projection(root)
                self.assertTrue(items, root)
                for key, cmd in items.items():
                    m = VERB.match(cmd)
                    self.assertTrue(m, cmd)
                    self.assertEqual(os.path.normcase(m.group("exe")), os.path.normcase(gateway_exe()))
                    self.assertEqual(m.group("id"), key.split("_", 1)[1])
                    for banned in ("powershell", "CONSOLES.ps1", "-File", "-WindowStyle"):
                        self.assertNotIn(banned.lower(), cmd.lower())

    def test_red_control_legacy_verb_fails_the_shape_gate(self):
        legacy = ('"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoLogo -NoProfile '
                  '-ExecutionPolicy Bypass -File "C:\\r\\Scripts\\consoles\\CONSOLES.ps1" -Profile claude-1 '
                  '-WorkDir "%V." -FromExplorer')
        self.assertIsNone(VERB.match(legacy))

    def test_verify_detects_stale_leaf_labels(self):
        self._check_leaf_drift("MUIVerb", "wrong account name", "label drift")

    def test_verify_detects_missing_provider_icons(self):
        self._check_leaf_drift("Icon", "missing-provider-icon.ico", "icon drift")

    def _check_leaf_drift(self, field, wrong, expected_error):
        base = STATE["classes"] + "\\Directory\\shell\\SaitulsAgents\\shell"
        rc, stdout, stderr = psraw("$key=Get-ChildItem -LiteralPath %s | Select-Object -First 1; "
                                  "$saved=$key.GetValue(%s); Set-ItemProperty -LiteralPath $key.PSPath -Name %s -Value %s; "
                                  "@{key=$key.PSPath;saved=$saved}|ConvertTo-Json -Compress" %
                                  (q(base), q(field), q(field), q(wrong)))
        self.assertEqual(rc, 0, stdout + stderr)
        saved = json.loads(stdout)
        try:
            result = subprocess.run([PS, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
                                     "-RegistryClassesRoot", STATE["classes"], "-GatewayDir", STATE["gwdir"],
                                     "-Federation", "off", "-VerifyOnly"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            self.assertNotEqual(result.returncode, 0, "the damaged owned cascade must fail verification")
            self.assertIn(expected_error, result.stdout + result.stderr)
        finally:
            psraw("Set-ItemProperty -LiteralPath %s -Name %s -Value %s" % (q(saved["key"]), q(field), q(saved["saved"])))

    def test_verify_detects_a_moved_root_in_the_sidecar(self):
        side = Path(STATE["gwdir"], "SaiConsoleLauncher.root.json")
        original = side.read_text(encoding="utf-8")
        try:
            side.write_text('{"root":"C:\\\\a-root-that-moved"}', encoding="utf-8")
            r = subprocess.run([PS, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER),
                                "-RegistryClassesRoot", STATE["classes"], "-GatewayDir", STATE["gwdir"],
                                "-Federation", "off", "-VerifyOnly"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("a-root-that-moved", r.stdout + r.stderr)
        finally:
            side.write_text(original, encoding="utf-8")


class NoFlashTests(unittest.TestCase):
    def test_gateway_is_a_gui_subsystem_binary(self):
        self.assertEqual(pe_subsystem(gateway_exe()), 2)

    def test_red_control_a_console_binary_fails_the_gui_gate(self):
        self.assertEqual(pe_subsystem(PS), 3)
        self.assertNotEqual(pe_subsystem(PS), 2)

    def test_preflight_and_shared_bootstrap_are_windowless_in_source(self):
        src = GATEWAY_SOURCE.read_text(encoding="utf-8")
        self.assertIn('psi.CreateNoWindow = true;', src)
        self.assertIn('psi.CreateNoWindow = origin == "shared";', src)
        self.assertIn("-target:winexe", INSTALLER.read_text(encoding="utf-8"))


class ExplorerMatrixTests(unittest.TestCase):
    def check(self, root_name, target, expected_dir):
        r = run_gateway(verb_for(root_name, "gw-ok", target))
        self.assertEqual(r.code, 0, r.log)
        self.assertIsNotNone(r.plan, r.log)
        self.assertEqual(r.plan["schema"], "saituls.console-launch/1")
        self.assertEqual(r.plan["profile"], "gw-ok")
        self.assertEqual(r.plan["workdir"], expected_dir)
        self.assertEqual(len(r.log), 1, r.log)           # exactly one attempt
        a = argv("powershell.exe " + r.plan["arguments"])
        self.assertEqual(a.count("-WorkDir"), 1, a)       # exactly once
        got = a[a.index("-WorkDir") + 1]
        self.assertEqual(os.path.normpath(got), os.path.normpath(expected_dir), a)
        self.assertEqual(a[a.index("-Profile") + 1], "gw-ok")
        self.assertIn("-FromExplorer", a)

    def test_directory_and_background_roots_for_every_name_class(self):
        for root_name in ("Directory", "Directory\\Background"):
            for kind, d in STATE["dirs"].items():
                with self.subTest(root=root_name, kind=kind):
                    self.check(root_name, d, d)

    def test_drive_root_keeps_its_trailing_backslash(self):
        drive = os.path.splitdrive(STATE["tmp"])[0] + "\\"
        self.check("Drive", drive, drive)

    def test_red_control_naive_argv_mangles_the_drive_root(self):
        # The trap the gateway's raw parser exists for: CommandLineToArgvW
        # reads the trailing \" of "C:\" as an escaped quote.
        self.assertNotEqual(argv('x.exe id "C:\\"')[2], "C:\\")


class FailureMatrixTests(unittest.TestCase):
    def fail_case(self, cmdline, category, code, **kw):
        r = run_gateway(cmdline, **kw)
        self.assertEqual(r.code, code, r.log)
        self.assertIsNone(r.plan)                       # nothing planned/launched
        self.assertEqual(len(r.log), 1, r.log)
        self.assertEqual(r.log[0]["outcome"], "failed")
        self.assertEqual(r.log[0]["category"], category)
        self.assertEqual(r.log[0]["schema"], "saituls.console-gateway/log/1")
        self.assertTrue(r.log[0]["reason"])
        return r

    def test_missing_root_via_override(self):
        empty = tempfile.mkdtemp(dir=STATE["tmp"])
        self.fail_case(verb_for("Directory", "gw-ok", STATE["dirs"]["ascii"]), "ROOT_MISSING", 10, root=empty)

    def test_moved_root_via_sidecar(self):
        iso = tempfile.mkdtemp(dir=STATE["tmp"], prefix="moved-")
        exe = os.path.join(iso, "SaiConsoleLauncher.exe")
        shutil.copy2(gateway_exe(), exe)
        Path(iso, "SaiConsoleLauncher.root.json").write_text('{"root":"C:\\\\gone-root-7f6a"}', encoding="utf-8")
        cmd = '"%s" gw-ok "%s"' % (exe, STATE["dirs"]["ascii"])
        self.fail_case(cmd, "ROOT_MISSING", 10, root=None)

    def test_invalid_profile_id(self):
        cmd = '"%s" bad;id "%s"' % (gateway_exe(), STATE["dirs"]["ascii"])
        self.fail_case(cmd, "PROFILE_INVALID", 11)

    def test_unknown_profile_id(self):
        self.fail_case(verb_for("Directory", "gw-nope", STATE["dirs"]["ascii"]), "PROFILE_UNKNOWN", 15)

    def test_missing_workdir(self):
        self.fail_case(verb_for("Directory", "gw-ok", os.path.join(STATE["tmp"], "no-such-dir")),
                       "WORKDIR_MISSING", 12)

    def test_inaccessible_workdir(self):
        d = tempfile.mkdtemp(dir=STATE["tmp"], prefix="locked-")
        user = os.environ["USERNAME"]
        subprocess.run(["icacls", d, "/deny", user + ":(OI)(CI)(RX)"], capture_output=True, check=True)
        try:
            self.fail_case(verb_for("Directory", "gw-ok", d), "WORKDIR_INACCESSIBLE", 13)
        finally:
            subprocess.run(["icacls", d, "/remove:d", user], capture_output=True)

    def test_missing_vendor_exe_fails_before_any_visible_session(self):
        # P0-5: no DRYRUN -- a pass here would really launch. The failure must
        # be decided by the hidden preflight, so no -FromExplorer console exists.
        marker = STATE["dirs"]["spaces"]
        r = self.fail_case(verb_for("Directory", "gw-missing", marker), "EXECUTABLE_MISSING", 15, dry=False)
        # The query's own command line contains both literals below, so it must
        # be excluded. Measured: without the exclusion this returns 1 with no
        # gateway run at all, an assertion that could never pass.
        rc, out, _ = psraw("@(Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
                           "Where-Object { $_.CommandLine -like '*-FromExplorer*' -and $_.CommandLine -like '*gw-missing*' "
                           "-and $_.CommandLine -notlike '*Get-CimInstance*' }).Count")
        self.assertEqual(out.strip(), "0")
        self.assertNotIn("child_pid", r.log[0]["request"])

    def test_red_control_a_good_request_does_not_fail(self):
        r = run_gateway(verb_for("Directory", "gw-ok", STATE["dirs"]["ascii"]))
        self.assertEqual(r.code, 0, r.log)
        self.assertEqual(r.log[0]["outcome"], "dryrun")


class RequestSchemaTests(unittest.TestCase):
    def write(self, schema):
        f = os.path.join(tempfile.mkdtemp(dir=STATE["tmp"]), "req.json")
        Path(f).write_text(json.dumps({"schema": schema, "profile": "gw-ok",
                                       "target": STATE["dirs"]["cyrillic"], "source": "explorer"}),
                           encoding="utf-8")
        return f

    def test_consumes_a_v1_request(self):
        r = run_gateway('"%s" --request "%s"' % (gateway_exe(), self.write("saituls.console-launch/1")))
        self.assertEqual(r.code, 0, r.log)
        self.assertEqual(r.plan["workdir"], STATE["dirs"]["cyrillic"])

    def test_red_control_rejects_an_unknown_schema_version(self):
        r = run_gateway('"%s" --request "%s"' % (gateway_exe(), self.write("saituls.console-launch/2")))
        self.assertEqual(r.code, 9)
        self.assertEqual(r.log[0]["category"], "REQUEST_INVALID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
