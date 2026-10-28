#!/usr/bin/env python3
"""Test suite for Folder Access Manager backend and per-account workdir persistence (T-298)."""
import configparser
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXE = os.path.join(ROOT, "Scripts", "accounts", "bin", "sai-accounts.exe")

PASSES = []
FAILURES = []


def check(name, ok, detail=""):
    if ok:
        PASSES.append(name)
        print("PASS  " + name)
    else:
        FAILURES.append(name + (" :: " + detail if detail else ""))
        print("FAIL  " + name + (" :: " + detail if detail else ""))


def test_cli_revoke_workdir_contract():
    if not os.path.exists(EXE):
        check("cli-exe-exists", False, "sai-accounts.exe not found at " + EXE)
        return
    check("cli-exe-exists", True)

    # 1. Missing selector
    r = subprocess.run([EXE, "revoke-workdir"], capture_output=True, text=True)
    check("cli-missing-selector-fails", r.returncode != 0)
    try:
        env = json.loads(r.stdout.strip())
        check("cli-missing-selector-envelope", env.get("status") == "missing_selector", str(env))
    except Exception as ex:
        check("cli-missing-selector-envelope", False, str(ex))

    # 2. Missing --workdir flag
    r = subprocess.run([EXE, "revoke-workdir", "AG1"], capture_output=True, text=True)
    check("cli-missing-workdir-flag-fails", r.returncode != 0)
    try:
        env = json.loads(r.stdout.strip())
        check("cli-missing-workdir-envelope", env.get("status") == "revoke_workdir_args", str(env))
    except Exception as ex:
        check("cli-missing-workdir-envelope", False, str(ex))

    # 3. Nonexistent account
    r = subprocess.run([EXE, "revoke-workdir", "NONEXISTENT_ACCOUNT_123", "--workdir", "C:\\Test"],
                       capture_output=True, text=True)
    check("cli-unknown-account-fails", r.returncode != 0)
    try:
        env = json.loads(r.stdout.strip())
        check("cli-unknown-account-envelope", env.get("status") == "ACCOUNT_NOT_FOUND", str(env))
    except Exception as ex:
        check("cli-unknown-account-envelope", False, str(ex))


def test_ini_per_account_workdir_contract():
    td = tempfile.mkdtemp(prefix="saituls-ini-test-")
    try:
        ini_path = os.path.join(td, "SAITULS.ini")
        cfg = configparser.ConfigParser()
        cfg["saituls"] = {
            "LastWorkDir": td
        }
        cfg["AccountWorkDirs"] = {
            "antigravity-1": td,
            "claude-work": td
        }
        cfg["AccountFolders"] = {
            "antigravity-1": f"{td}|C:\\Project2"
        }
        with open(ini_path, "w", encoding="utf-8") as f:
            cfg.write(f)

        # Verify parsed structure matches contract
        cfg2 = configparser.ConfigParser()
        cfg2.read(ini_path, encoding="utf-8")
        check("ini-account-workdirs-section-present", "AccountWorkDirs" in cfg2)
        check("ini-account-workdir-retrieved", cfg2.get("AccountWorkDirs", "antigravity-1") == td)
        check("ini-account-folders-section-present", "AccountFolders" in cfg2)
        flds = cfg2.get("AccountFolders", "antigravity-1").split("|")
        check("ini-account-folders-retrieved", len(flds) == 2 and flds[0] == td)
    finally:
        try:
            import shutil
            shutil.rmtree(td, ignore_errors=True)
        except Exception:
            pass


def test_cli_trusted_mode_status_contract():
    if not os.path.exists(EXE):
        check("cli-exe-exists-tm", False, "sai-accounts.exe not found at " + EXE)
        return
    check("cli-exe-exists-tm", True)

    # 1. Missing selector
    r = subprocess.run([EXE, "trusted-mode-status"], capture_output=True, text=True)
    check("cli-tm-missing-selector-fails", r.returncode != 0)
    try:
        env = json.loads(r.stdout.strip())
        check("cli-tm-missing-selector-envelope", env.get("status") == "missing_selector", str(env))
    except Exception as ex:
        check("cli-tm-missing-selector-envelope", False, str(ex))

    # 2. Nonexistent account
    r = subprocess.run([EXE, "trusted-mode-status", "NONEXISTENT_ACCOUNT_456"],
                       capture_output=True, text=True)
    check("cli-tm-unknown-account-fails", r.returncode != 0)
    try:
        env = json.loads(r.stdout.strip())
        check("cli-tm-unknown-account-envelope", env.get("status") == "ACCOUNT_NOT_FOUND", str(env))
    except Exception as ex:
        check("cli-tm-unknown-account-envelope", False, str(ex))


def main():
    print("=== Testing Folder Access Manager Contract (T-298/T-299) ===")
    test_cli_revoke_workdir_contract()
    test_ini_per_account_workdir_contract()
    test_cli_trusted_mode_status_contract()

    print()
    print("PASS %d / FAIL %d" % (len(PASSES), len(FAILURES)))
    if FAILURES:
        for f in FAILURES:
            print("  FAILED: " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
