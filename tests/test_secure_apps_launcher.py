#!/usr/bin/env python3
"""Secure Apps launcher: the SAITULS Tools -> Secure Apps click really starts it.

Run:  python tests/test_secure_apps_launcher.py
Exit: 0 = all PASS (SKIP off Windows), 1 = failures.

Runs Scripts/secure_apps/SECURE_APPS.ps1's LAUNCH path (not -SelfTest) in
Windows PowerShell -- the host SAITULS uses -- with Start-Process and the
error MessageBox swapped for prints, so no window ever opens. T-192 found the
launch path dead twice over: ``& (if ...)`` is not an expression in
PowerShell, and the one-line Python preflight chained for/try with ';'. Both
threw into a console that closed at once, so the click "did nothing".

  1. clean launch reaches Start-Process with pythonw + secure_apps.pyw, exit 0
  2. a missing dependency surfaces as the visible error (MessageBox path), exit 1
  3. mutants of both original defects are caught by check 1
"""
import os, shutil, subprocess, sys, tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
SA_DIR = os.path.join(ROOT, 'Scripts', 'secure_apps')
LAUNCHER = os.path.join(SA_DIR, 'SECURE_APPS.ps1')

START = "Start-Process -FilePath $exe -ArgumentList $guiArgs -WorkingDirectory $ScriptDir"
BOX = "[void][System.Windows.Forms.MessageBox]::Show($msg, 'SAITULS - Secure Apps', 'OK', 'Error')"
SCRIPTDIR = "$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition"


def harness(text):
    for needle in (START, BOX, SCRIPTDIR):
        if needle not in text:
            raise AssertionError('launcher changed shape; missing: %s' % needle)
    return (text.replace(SCRIPTDIR, "$ScriptDir = '%s'" % SA_DIR)
                .replace(START, 'Write-Host "WOULD START: $exe $guiArgs"')
                .replace(BOX, "Write-Host 'WOULD SHOW MESSAGEBOX'"))


def run(text, tmp, name):
    path = os.path.join(tmp, name)
    with open(path, 'w', encoding='utf-8-sig', newline='\r\n') as f:
        f.write(text)
    r = subprocess.run(['powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-File', path], capture_output=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).decode('utf-8', 'replace')


def launches(rc, out):
    return rc == 0 and 'WOULD START:' in out and 'secure_apps.pyw' in out \
        and 'could not start' not in out


def main():
    if os.name != 'nt' or not shutil.which('powershell.exe'):
        print('SKIP Windows PowerShell not available')
        return 0
    src = open(LAUNCHER, encoding='utf-8-sig').read().replace('\r\n', '\n')
    base = harness(src)
    fails = 0
    tmp = tempfile.mkdtemp(prefix='sa-launcher-')
    try:
        rc, out = run(base, tmp, 'clean.ps1')
        ok = launches(rc, out)
        print(('PASS' if ok else 'FAIL') + ' clean launch reaches Start-Process (rc=%d)' % rc)
        if not ok:
            print(out.strip()); fails += 1

        missing = base.replace("for m in ('PyQt6'", "for m in ('no_such_module_t192', 'PyQt6'")
        rc, out = run(missing, tmp, 'missing.ps1')
        ok = (rc == 1 and 'no_such_module_t192' in out and 'WOULD SHOW MESSAGEBOX' in out
              and 'WOULD START' not in out)
        print(('PASS' if ok else 'FAIL') + ' missing dependency shows the error box, no launch (rc=%d)' % rc)
        if not ok:
            print(out.strip()); fails += 1

        mutants = {
            "'& (if ...)' probe": base.replace(
                "$probeExe = if ($python) { $python } else { $exe }",
                "$probeExe = 'unused'").replace(
                "& $probeExe -c $preflightCmd",
                "& (if ($python) { $python } else { $exe }) -c $preflightCmd"),
            "one-line ';'-chained preflight": base.replace(
                "for m in ('PyQt6', 'cryptography', 'win32api'):\n    try:\n        __import__(m)",
                "missing = []; for m in ('PyQt6', 'cryptography', 'win32api'):\n    try:\n        __import__(m)"),
        }
        for name, text in mutants.items():
            if text == base:
                print('FAIL mutant did not apply: %s' % name); fails += 1
                continue
            rc, out = run(text, tmp, 'mutant.ps1')
            caught = not launches(rc, out)
            print(('PASS' if caught else 'FAIL') + ' mutant caught: %s' % name)
            fails += not caught
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print('\n%s: %d failure(s)' % ('PASS' if not fails else 'FAIL', fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
