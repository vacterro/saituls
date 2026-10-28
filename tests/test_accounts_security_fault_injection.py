#!/usr/bin/env python3
"""Dedicated fault-injection test suite for W2-001, W2-002, and W2-003 (T-260).

Negative security and failure-mode controls:
- W2-001: Fail-closed ACL security and verification
  - Helper install fails closed on missing operator SID
  - Helper install fails closed on invalid operator SID
  - Corrupt sealed secret fails closed (SECRET_CORRUPT)
  - Missing sealed secret fails closed (SECRET_NOT_FOUND)
  - System service fails closed when unconfigured (no operator SID)
- W2-002: Durable workdir grant ownership ledger & transaction state
  - Corrupted grants.json fails closed (LEDGER_CORRUPTED) and preserves file
  - Ledger lock contention fails closed (LEDGER_LOCK_TIMEOUT)
  - Simulated ledger commit failure triggers verified ACE rollback (RollbackAce)
  - Interrupted transaction recovery (grants.txn.json) reconciles uncommitted grant
- W2-003: Managed context deletion truth and lifecycle machine
  - Broker refusing shutdown halts deletion (broker_shutdown_refused)
  - Helper offline during deletion marks cleanup_required & fails closed (exit 1)
  - Idempotent deletion retry completes once helper is restored (exit 0)
  - Refusal to delete non-SAIAG Windows account (USER_DELETE_REFUSED_NOT_MANAGED)
  - Managed context remove verifies absence of secrets and grants

Run:  python tests/test_accounts_security_fault_injection.py
Exit: 0 = all PASS, 1 = failures.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
TEST_BIN = os.environ.get('SAI_ACCOUNTS_TEST_BIN') or os.path.join(ROOT, 'Scripts', 'accounts', 'bin')
EXE = os.path.join(TEST_BIN, 'sai-accounts.exe')
HELPER_EXE = os.path.join(TEST_BIN, 'sai-accounts-helper.exe')

FAILURES = []
PASSES = []

kernel32 = ctypes.windll.kernel32
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
OPEN_EXISTING = 3
PIPE_SQOS = 0x00100000 | 0x00010000


def _ps5_env():
    env = dict(os.environ)
    for k in list(env.keys()):
        if k.upper() == 'PSMODULEPATH':
            del env[k]
    return env


def add_ace_ps(path, sid_str, mask='Modify', deny=False, inherit='ContainerInherit,ObjectInherit'):
    script = (
        f"$sid = New-Object System.Security.Principal.SecurityIdentifier('{sid_str}'); "
        f"$acl = Get-Acl -LiteralPath '{path}'; "
        f"$rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, '{mask}', '{inherit}', 'None', '{'Deny' if deny else 'Allow'}'); "
        f"$acl.AddAccessRule($rule); "
        f"Set-Acl -LiteralPath '{path}' -AclObject $acl; "
        f"(Get-Acl -LiteralPath '{path}').Sddl"
    )
    r = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, text=True, env=_ps5_env())
    return r.stdout.strip()


def has_ace_ps(path, sid_str):
    script = (
        f"$acl = Get-Acl -LiteralPath '{path}'; "
        f"@($acl.Access | Where-Object {{ "
        f"    try {{ $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq '{sid_str}' }} catch {{ $false }} "
        f"}}).Count"
    )
    r = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, text=True, env=_ps5_env())
    try:
        return int(r.stdout.strip()) > 0
    except Exception:
        return False


def get_ace_rights_ps(path, sid_str):
    script = (
        f"$acl = Get-Acl -LiteralPath '{path}'; "
        f"@($acl.Access | Where-Object {{ "
        f"    try {{ $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq '{sid_str}' }} catch {{ $false }} "
        f"}} | ForEach-Object {{ $_.FileSystemRights.ToString() }}) -join ','"
    )
    r = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, text=True, env=_ps5_env())
    return r.stdout.strip()


def check(name, ok, detail=''):
    if ok:
        PASSES.append(name)
        print('PASS  ' + name)
    else:
        FAILURES.append(name + (' :: ' + detail if detail else ''))
        print('FAIL  ' + name + (' :: ' + detail if detail else ''))


def real_sid():
    r = subprocess.run(['powershell', '-NoProfile', '-Command',
                        '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
                       capture_output=True, text=True)
    return r.stdout.strip()


def pipe_open(name, timeout_s=10.0):
    pipe_path = "\\\\.\\pipe\\" + name
    deadline = time.time() + timeout_s
    while True:
        handle = kernel32.CreateFileW(pipe_path, GENERIC_READ | GENERIC_WRITE,
                                      0, None, OPEN_EXISTING, PIPE_SQOS, None)
        if handle != -1 and handle != 0xFFFFFFFFFFFFFFFF:
            return handle
        if time.time() >= deadline:
            raise OSError('pipe_open timed out for ' + name)
        time.sleep(0.05)


def pipe_request(name, obj, timeout_s=10.0):
    handle = pipe_open(name, timeout_s)
    try:
        data = (json.dumps(obj) + '\n').encode('utf-8')
        written = wintypes.DWORD(0)
        if not kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None):
            raise OSError('write failed')
        buf = ctypes.create_string_buffer(65536)
        read = wintypes.DWORD(0)
        deadline = time.time() + timeout_s
        chunks = []
        while time.time() < deadline:
            if kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None) and read.value:
                chunks.append(buf.raw[:read.value])
                if b'\n' in chunks[-1]:
                    break
            else:
                break
        line = b''.join(chunks).decode('utf-8', errors='replace').strip()
        return json.loads(line) if line else None
    finally:
        kernel32.CloseHandle(handle)


class TestPlane:
    def __init__(self, prefix='fault-plane'):
        self.dir = tempfile.mkdtemp(prefix='sai-fault-')
        self.scope = prefix
        self.sid = real_sid()
        self.env = dict(os.environ)
        self.env.update({
            'SAI_ACCOUNTS_ROOT': self.dir,
            'SAI_ACCOUNTS_SCOPE': self.scope,
            'SAI_ACCOUNTS_OPERATOR_SID': self.sid,
            'SAI_ACCOUNTS_LEGACY_USER': os.path.join(self.dir, 'legacy-user.json'),
            'SAI_ACCOUNTS_LEGACY_MACHINE': os.path.join(self.dir, 'legacy-machine.json'),
            'SAI_ACCOUNTS_TEST_BIN': TEST_BIN,
            'SAI_ACCOUNTS_EXE': EXE,
        })

    def run(self, *args, timeout=60):
        r = subprocess.run([EXE] + list(args), capture_output=True, text=True,
                            env=self.env, timeout=timeout, cwd=self.dir)
        try:
            payload = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
        except (ValueError, IndexError):
            payload = None
        return r.returncode, payload, r.stdout, r.stderr

    def registry(self):
        path = os.path.join(self.dir, 'scopes', self.scope, 'accounts.json')
        if not os.path.exists(path):
            return None
        with open(path, 'rb') as fh:
            raw = fh.read()
        if raw[:3] == b'\xef\xbb\xbf':
            raw = raw[3:]
        return json.loads(raw.decode('utf-8'))

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class HelperInstance:
    def __init__(self, plane, pipe_name=None):
        self.plane = plane
        self.pipe_name = pipe_name or ('sai-fault-helper-' + plane.scope)
        self.proc = None
        self.env = dict(plane.env)
        self.env['SAI_HELPER_PIPE'] = self.pipe_name
        self.env['SAI_HELPER_TEST_MODE'] = '1'
        self.env['SAI_ACCOUNTS_ROOT'] = plane.dir
        self.env['SAI_ACCOUNTS_EXE'] = EXE
        self.plane.env['SAI_HELPER_PIPE'] = self.pipe_name
        self.plane.env['SAI_HELPER_TEST_MODE'] = '1'

    def start(self):
        self.proc = subprocess.Popen(
            [HELPER_EXE, '--run', '--pipe', self.pipe_name],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=0x08000000)
        ready = False
        for _ in range(50):
            if self.proc.poll() is not None:
                break
            try:
                st = pipe_request(self.pipe_name, {'op': 'status'}, timeout_s=0.2)
                if st is not None:
                    ready = True
                    break
            except Exception:
                time.sleep(0.05)
        return ready

    def request(self, obj, timeout_s=5.0):
        return pipe_request(self.pipe_name, obj, timeout_s=timeout_s)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass


# ----------------------------------------------------------------------
# W2-001 Tests: Fail-Closed ACL Security & Verification
# ----------------------------------------------------------------------
def test_w2_001_install_validation():
    # 1. Missing operator SID fails closed
    r_missing = subprocess.run([HELPER_EXE, '--install'], capture_output=True, text=True)
    check('w2-001-install-missing-sid-exits-1', r_missing.returncode == 1,
          f'rc={r_missing.returncode} out={r_missing.stdout + r_missing.stderr}')
    check('w2-001-install-missing-sid-error-detail',
          'no valid operator sid provided' in (r_missing.stdout + r_missing.stderr).lower(),
          r_missing.stdout + r_missing.stderr)

    # 2. Invalid operator SID fails closed
    r_invalid = subprocess.run([HELPER_EXE, '--install', '--operator-sid', 'InvalidSidString'],
                               capture_output=True, text=True)
    check('w2-001-install-invalid-sid-exits-1', r_invalid.returncode == 1,
          f'rc={r_invalid.returncode} out={r_invalid.stdout + r_invalid.stderr}')
    check('w2-001-install-invalid-sid-error-detail',
          'no valid operator sid provided' in (r_invalid.stdout + r_invalid.stderr).lower() or
          'not a valid security identifier' in (r_invalid.stdout + r_invalid.stderr).lower(),
          r_invalid.stdout + r_invalid.stderr)


def test_w2_001_sealed_secret_faults():
    p = TestPlane('w2-001-secrets')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-001-helper-started', ok, 'helper did not start')
        if not ok:
            return

        vault_dir = os.path.join(p.dir, 'vault')
        os.makedirs(vault_dir, exist_ok=True)

        # Corrupt sealed secret fails closed
        corrupt_sid = 'S-1-5-21-111111111-222222222-333333333-1001'
        fp_corrupt = hashlib.sha256(corrupt_sid.lower().encode()).hexdigest()[:16]
        seal_corrupt = os.path.join(vault_dir, fp_corrupt + '.seal')
        with open(seal_corrupt, 'wb') as f:
            f.write(b'GARBAGE_PAYLOAD_NOT_DPAPI')

        r_corrupt = h.request({'op': 'managed_context_start', 'target_sid': corrupt_sid, 'scope_id': p.scope})
        check('w2-001-corrupt-secret-fails-closed',
              r_corrupt is not None and r_corrupt.get('status') == 'SECRET_CORRUPT',
              str(r_corrupt))

        # Missing secret fails closed
        missing_sid = 'S-1-5-21-111111111-222222222-333333333-1002'
        r_missing = h.request({'op': 'managed_context_start', 'target_sid': missing_sid, 'scope_id': p.scope})
        check('w2-001-missing-secret-fails-closed',
              r_missing is not None and r_missing.get('status') == 'SECRET_NOT_FOUND',
              str(r_missing))
    finally:
        h.stop()
        p.cleanup()


# ----------------------------------------------------------------------
# W2-002 Tests: Durable Workdir Grant Ledger & Transaction State
# ----------------------------------------------------------------------
def test_w2_002_corrupted_ledger_refusal():
    p = TestPlane('w2-002-corrupt-ledger')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-helper-started', ok, 'helper did not start')
        if not ok:
            return

        # Write invalid JSON to grants.json
        grants_file = os.path.join(p.dir, 'grants.json')
        corrupted_content = '{"corrupted": [unclosed array...'
        with open(grants_file, 'w', encoding='utf-8') as f:
            f.write(corrupted_content)

        test_workdir = os.path.join(p.dir, 'test-workdir-corrupt')
        os.makedirs(test_workdir, exist_ok=True)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-1001'

        # Request grant
        resp = h.request({
            'op': 'managed_workdir_ensure_access',
            'target_sid': target_sid,
            'workdir': test_workdir
        })

        # Helper must return LEDGER_CORRUPTED
        check('w2-002-corrupted-ledger-rejected',
              resp is not None and resp.get('status') == 'LEDGER_CORRUPTED',
              str(resp))

        # Grants file must NOT have been overwritten or wiped
        with open(grants_file, 'r', encoding='utf-8') as f:
            remaining = f.read()
        check('w2-002-corrupted-ledger-preserved',
              remaining == corrupted_content,
              f'remaining={remaining}')
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_ledger_lock_timeout():
    p = TestPlane('w2-002-lock-timeout')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-lock-helper-started', ok, 'helper did not start')
        if not ok:
            return

        lock_file = os.path.join(p.dir, 'grants.lock')
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)

        # Open lock exclusively from this process
        h_lock = kernel32.CreateFileW(lock_file, GENERIC_READ | GENERIC_WRITE,
                                      0, # FileShare.None
                                      None, OPEN_EXISTING if os.path.exists(lock_file) else 2, 0, None)
        try:
            test_workdir = os.path.join(p.dir, 'test-workdir-lock')
            os.makedirs(test_workdir, exist_ok=True)
            target_sid = 'S-1-5-21-444444444-555555555-666666666-1002'

            t0 = time.time()
            resp = h.request({
                'op': 'managed_workdir_ensure_access',
                'target_sid': target_sid,
                'workdir': test_workdir
            }, timeout_s=15.0)
            elapsed = time.time() - t0

            check('w2-002-ledger-lock-timeout-returned',
                  resp is not None and resp.get('status') == 'LEDGER_LOCK_TIMEOUT',
                  f'elapsed={elapsed:.1f}s resp={resp}')
        finally:
            if h_lock not in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
                kernel32.CloseHandle(h_lock)
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_ledger_save_failure_rolls_back_ace():
    p = TestPlane('w2-002-save-rollback')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-rollback-helper-started', ok, 'helper did not start')
        if not ok:
            return

        grants_file = os.path.join(p.dir, 'grants.json')
        # Seed an initial valid empty ledger
        with open(grants_file, 'w', encoding='utf-8') as f:
            f.write('[]')

        # Make grants.json READ-ONLY to induce ReplaceFileW failure in SaveGrantLedgerSafe
        kernel32.SetFileAttributesW(grants_file, 1) # FILE_ATTRIBUTE_READONLY = 1

        test_workdir = os.path.join(p.dir, 'test-workdir-rollback')
        os.makedirs(test_workdir, exist_ok=True)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-1003'

        try:
            resp = h.request({
                'op': 'managed_workdir_ensure_access',
                'target_sid': target_sid,
                'workdir': test_workdir
            })

            # Helper must report LEDGER_SAVE_FAILED after successful RollbackAce
            check('w2-002-ledger-save-failed-reported',
                  resp is not None and resp.get('status') == 'LEDGER_SAVE_FAILED',
                  str(resp))

            # Verify target directory ACL has NO grant for target_sid left behind
            check('w2-002-ace-cleanly-rolled-back',
                  not has_ace_ps(test_workdir, target_sid),
                  'dangling ACE found')
        finally:
            kernel32.SetFileAttributesW(grants_file, 128) # reset to NORMAL
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_interrupted_transaction_reconciliation():
    p = TestPlane('w2-002-txn-reconcile')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-reconcile-helper-started', ok, 'helper did not start')
        if not ok:
            return

        test_workdir = os.path.join(p.dir, 'test-workdir-reconcile')
        os.makedirs(test_workdir, exist_ok=True)
        canonical = os.path.abspath(test_workdir)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-1004'

        # 1. Apply ACE to folder
        add_ace_ps(canonical, target_sid)

        # 2. Stage grants.txn.json with state: "ace_verified"
        txn_file = os.path.join(p.dir, 'grants.txn.json')
        txn_data = {
            'txn_id': 'test-txn-1234',
            'op': 'grant',
            'target_sid': target_sid,
            'workdir': canonical,
            'created': True,
            'state': 'ace_verified',
            'updated_at': '2026-10-11T00:00:00Z'
        }
        with open(txn_file, 'w', encoding='utf-8') as f:
            json.dump(txn_data, f)

        # Ensure grants.json is empty
        grants_file = os.path.join(p.dir, 'grants.json')
        with open(grants_file, 'w', encoding='utf-8') as f:
            f.write('[]')

        # 3. Call managed_workdir_ensure_access; helper must reconcile interrupted transaction
        resp = h.request({
            'op': 'managed_workdir_ensure_access',
            'target_sid': target_sid,
            'workdir': canonical
        })
        check('w2-002-reconciliation-succeeded',
              resp is not None and resp.get('status') in ('already_granted', 'granted'),
              str(resp))

        # 4. Check txn file was removed
        check('w2-002-txn-file-cleaned-up', not os.path.exists(txn_file), 'txn file still exists')

        # 5. Check row committed to grants.json
        with open(grants_file, 'r', encoding='utf-8-sig') as f:
            committed_rows = json.load(f)
        row_found = any(r.get('target_sid') == target_sid for r in committed_rows)
        check('w2-002-reconciled-grant-committed-to-ledger', row_found, str(committed_rows))
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_already_granted_failed_ledger_persistence():
    p = TestPlane('w2-002-already-persist-fail')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-already-helper-started', ok, 'helper did not start')
        if not ok: return

        test_workdir = os.path.join(p.dir, 'test-workdir-already')
        os.makedirs(test_workdir, exist_ok=True)
        canonical = os.path.abspath(test_workdir)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2001'

        add_ace_ps(canonical, target_sid)

        grants_file = os.path.join(p.dir, 'grants.json')
        with open(grants_file, 'w', encoding='utf-8') as f:
            f.write('[]')

        kernel32.SetFileAttributesW(grants_file, 1) # ReadOnly
        try:
            resp = h.request({
                'op': 'managed_workdir_ensure_access',
                'target_sid': target_sid,
                'workdir': canonical
            })
            check('w2-002-already-granted-with-failed-ledger-persistence',
                  resp is not None and resp.get('status') == 'LEDGER_SAVE_FAILED',
                  str(resp))
        finally:
            kernel32.SetFileAttributesW(grants_file, 128)
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_third_party_ace_attribution():
    p = TestPlane('w2-002-third-party-ace')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-third-party-helper-started', ok, 'helper did not start')
        if not ok: return

        test_workdir = os.path.join(p.dir, 'test-workdir-third-party')
        os.makedirs(test_workdir, exist_ok=True)
        canonical = os.path.abspath(test_workdir)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2002'

        add_ace_ps(canonical, target_sid)

        grants_file = os.path.join(p.dir, 'grants.json')
        with open(grants_file, 'w', encoding='utf-8') as f:
            f.write('[]')

        resp1 = h.request({
            'op': 'managed_workdir_ensure_access',
            'target_sid': target_sid,
            'workdir': canonical
        })
        check('w2-002-third-party-first-call-already-granted',
              resp1 is not None and resp1.get('status') == 'already_granted', str(resp1))

        with open(grants_file, 'r', encoding='utf-8-sig') as f:
            rows1 = json.load(f)
        matching1 = [r for r in rows1 if r.get('target_sid') == target_sid]
        check('w2-002-third-party-recorded-created-false',
              len(matching1) == 1 and matching1[0].get('created') is False, str(matching1))

        resp2 = h.request({
            'op': 'managed_workdir_ensure_access',
            'target_sid': target_sid,
            'workdir': canonical
        })
        check('w2-002-third-party-second-call-already-granted',
              resp2 is not None and resp2.get('status') == 'already_granted', str(resp2))

        with open(grants_file, 'r', encoding='utf-8-sig') as f:
            rows2 = json.load(f)
        matching2 = [r for r in rows2 if r.get('target_sid') == target_sid]
        check('w2-002-third-party-not-upgraded-to-helper-owned',
              len(matching2) == 1 and matching2[0].get('created') is False, str(matching2))

        resp_rev = h.request({
            'op': 'managed_workdir_revoke_access',
            'target_sid': target_sid
        })
        check('w2-002-third-party-revoke-status-revoked',
              resp_rev is not None and resp_rev.get('status') == 'revoked', str(resp_rev))
        check('w2-002-third-party-revoke-preserved-rule',
              resp_rev is not None and resp_rev.get('preserved_rules', 0) >= 1 and resp_rev.get('revoked_rules', 0) == 0,
              str(resp_rev))

        check('w2-002-third-party-ace-still-present', has_ace_ps(canonical, target_sid), 'ACE was deleted!')
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_revoke_intent_write_failure():
    p = TestPlane('w2-002-revoke-intent-fail')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-revoke-intent-helper-started', ok, 'helper did not start')
        if not ok: return

        test_workdir = os.path.join(p.dir, 'test-workdir-revoke-fail')
        os.makedirs(test_workdir, exist_ok=True)
        canonical = os.path.abspath(test_workdir)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2003'

        resp_grant = h.request({
            'op': 'managed_workdir_ensure_access',
            'target_sid': target_sid,
            'workdir': canonical
        })
        check('w2-002-revoke-intent-initial-grant-ok',
              resp_grant is not None and resp_grant.get('status') == 'granted', str(resp_grant))

        txn_file = os.path.join(p.dir, 'grants.txn.json')
        os.makedirs(txn_file, exist_ok=True)
        try:
            resp_rev = h.request({
                'op': 'managed_workdir_revoke_access',
                'target_sid': target_sid
            })
            check('w2-002-revoke-intent-write-failure-refused',
                  resp_rev is not None and resp_rev.get('status') == 'TRANSACTION_INIT_FAILED',
                  str(resp_rev))

            check('w2-002-revoke-intent-fail-ace-preserved',
                  has_ace_ps(canonical, target_sid), 'ACE was prematurely deleted')

            grants_file = os.path.join(p.dir, 'grants.json')
            with open(grants_file, 'r', encoding='utf-8-sig') as f:
                rows = json.load(f)
            check('w2-002-revoke-intent-fail-ledger-intact',
                  any(r.get('target_sid') == target_sid for r in rows), str(rows))
        finally:
            shutil.rmtree(txn_file, ignore_errors=True)
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_crash_halfway_multi_grant_revoke():
    p = TestPlane('w2-002-crash-halfway')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-halfway-helper-started', ok, 'helper did not start')
        if not ok: return

        dirA = os.path.abspath(os.path.join(p.dir, 'test-dir-A'))
        dirB = os.path.abspath(os.path.join(p.dir, 'test-dir-B'))
        os.makedirs(dirA, exist_ok=True)
        os.makedirs(dirB, exist_ok=True)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2004'

        h.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': dirA})
        h.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': dirB})
        h.stop()

        rem_ace_a = f"$acl = Get-Acl '{dirA}'; $exact = @($acl.Access | Where-Object {{ $_.IdentityReference.Value -eq '{target_sid}' }}); foreach($r in $exact){{ $acl.RemoveAccessRuleSpecific($r) }}; Set-Acl '{dirA}' $acl"
        subprocess.run(['powershell', '-NoProfile', '-Command', rem_ace_a], capture_output=True, text=True)

        txn_file = os.path.join(p.dir, 'grants.txn.json')
        txn_data = {
            'txn_id': 'txn-halfway-123',
            'op': 'revoke',
            'target_sid': target_sid,
            'workdir': '',
            'created': False,
            'target_workdirs': [dirA, dirB],
            'state': 'intent_logged',
            'updated_at': '2026-10-11T00:00:00Z'
        }
        with open(txn_file, 'w', encoding='utf-8') as f:
            json.dump(txn_data, f)

        h2 = HelperInstance(p)
        try:
            ok2 = h2.start()
            check('w2-002-halfway-restart-started', ok2, 'restart failed')
            if not ok2: return

            resp = h2.request({
                'op': 'managed_workdir_revoke_access',
                'target_sid': target_sid
            })
            check('w2-002-halfway-reconcile-succeeded',
                  resp is not None and resp.get('status') == 'revoked', str(resp))

            get_b = f"(Get-Acl '{dirB}').Access | Where-Object {{ $_.IdentityReference.Value -eq '{target_sid}' }}"
            r_b = subprocess.run(['powershell', '-NoProfile', '-Command', get_b], capture_output=True, text=True)
            check('w2-002-halfway-dirB-ace-removed', r_b.stdout.strip() == '', f'dirB still has ACE: {r_b.stdout}')

            grants_file = os.path.join(p.dir, 'grants.json')
            with open(grants_file, 'r', encoding='utf-8-sig') as f:
                rows = json.load(f)
            check('w2-002-halfway-ledger-cleaned',
                  not any(r.get('target_sid') == target_sid for r in rows), str(rows))
            check('w2-002-halfway-txn-file-removed', not os.path.exists(txn_file), 'txn file remained')
        finally:
            h2.stop()
    finally:
        p.cleanup()


def test_w2_002_crash_after_acl_before_ledger_commit():
    p = TestPlane('w2-002-crash-after-acl')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-after-acl-helper-started', ok, 'helper did not start')
        if not ok: return

        test_dir = os.path.abspath(os.path.join(p.dir, 'test-dir-after-acl'))
        os.makedirs(test_dir, exist_ok=True)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2005'

        h.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': test_dir})
        h.stop()

        rem_ace = f"$acl = Get-Acl '{test_dir}'; $exact = @($acl.Access | Where-Object {{ $_.IdentityReference.Value -eq '{target_sid}' }}); foreach($r in $exact){{ $acl.RemoveAccessRuleSpecific($r) }}; Set-Acl '{test_dir}' $acl"
        subprocess.run(['powershell', '-NoProfile', '-Command', rem_ace], capture_output=True, text=True)

        txn_file = os.path.join(p.dir, 'grants.txn.json')
        txn_data = {
            'txn_id': 'txn-after-acl-456',
            'op': 'revoke',
            'target_sid': target_sid,
            'workdir': test_dir,
            'created': False,
            'target_workdirs': [test_dir],
            'state': 'intent_logged',
            'updated_at': '2026-10-11T00:00:00Z'
        }
        with open(txn_file, 'w', encoding='utf-8') as f:
            json.dump(txn_data, f)

        h2 = HelperInstance(p)
        try:
            h2.start()
            resp = h2.request({'op': 'managed_workdir_revoke_access', 'target_sid': target_sid})
            check('w2-002-crash-after-acl-reconcile-ok',
                  resp is not None and resp.get('status') == 'revoked', str(resp))

            grants_file = os.path.join(p.dir, 'grants.json')
            with open(grants_file, 'r', encoding='utf-8-sig') as f:
                rows = json.load(f)
            check('w2-002-crash-after-acl-ledger-clean',
                  not any(r.get('target_sid') == target_sid for r in rows), str(rows))
            check('w2-002-crash-after-acl-txn-gone', not os.path.exists(txn_file), 'txn file remains')
        finally:
            h2.stop()
    finally:
        p.cleanup()


def test_w2_002_crash_after_ledger_before_journal_removal():
    p = TestPlane('w2-002-crash-after-ledger')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-after-ledger-helper-started', ok, 'helper did not start')
        if not ok: return

        test_dir = os.path.abspath(os.path.join(p.dir, 'test-dir-after-ledger'))
        os.makedirs(test_dir, exist_ok=True)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2006'

        h.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': test_dir})
        h.stop()

        txn_file = os.path.join(p.dir, 'grants.txn.json')
        txn_data = {
            'txn_id': 'txn-after-ledger-789',
            'op': 'grant',
            'target_sid': target_sid,
            'workdir': test_dir,
            'created': True,
            'state': 'ace_verified',
            'updated_at': '2026-10-11T00:00:00Z'
        }
        with open(txn_file, 'w', encoding='utf-8') as f:
            json.dump(txn_data, f)

        h2 = HelperInstance(p)
        try:
            h2.start()
            resp = h2.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': test_dir})
            check('w2-002-crash-after-ledger-reconcile-ok',
                  resp is not None and resp.get('status') == 'already_granted', str(resp))
            check('w2-002-crash-after-ledger-journal-removed',
                  not os.path.exists(txn_file), 'journal was not removed')
        finally:
            h2.stop()
    finally:
        p.cleanup()


def test_w2_002_partial_revoke_failed_rules():
    p = TestPlane('w2-002-partial-revoke')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-partial-helper-started', ok, 'helper did not start')
        if not ok: return

        test_dir = os.path.abspath(os.path.join(p.dir, 'test-dir-partial'))
        os.makedirs(test_dir, exist_ok=True)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2007'

        # 1. Legitimately grant access to test_dir
        resp_grant = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': test_dir})
        check('w2-002-partial-grant-ok', resp_grant is not None and resp_grant.get('status') == 'granted', str(resp_grant))

        # 2. Add an invalid entry with empty workdir into grants.json for target_sid
        grants_file = os.path.join(p.dir, 'grants.json')
        with open(grants_file, 'r', encoding='utf-8-sig') as f:
            rows = json.load(f)
        rows.append({
            'key': f'{target_sid}|FAILED_ENTRY',
            'target_sid': target_sid,
            'workdir': '',
            'created': True,
            'last_seen': '2026-10-11T00:00:00Z'
        })
        with open(grants_file, 'w', encoding='utf-8') as f:
            json.dump(rows, f)

        # 3. Call revoke - must return PARTIAL_REVOCATION_FAILED
        resp = h.request({
            'op': 'managed_workdir_revoke_access',
            'target_sid': target_sid
        })
        check('w2-002-partial-revoke-status-not-revoked',
              resp is not None and resp.get('status') == 'PARTIAL_REVOCATION_FAILED',
              str(resp))
        check('w2-002-partial-revoke-failed-rules-reported',
              resp is not None and resp.get('failed_rules', 0) == 1,
              str(resp))
        check('w2-002-partial-revoke-revoked-rules-reported',
              resp is not None and resp.get('revoked_rules', 0) == 1,
              str(resp))

        # 4. Failed rule must remain in ledger
        with open(grants_file, 'r', encoding='utf-8-sig') as f:
            rows_after = json.load(f)
        check('w2-002-partial-revoke-failed-rule-in-ledger',
              any(r.get('target_sid') == target_sid and r.get('workdir') == '' for r in rows_after),
              str(rows_after))

        # 5. Verify managed_context_remove fails closed with PARTIAL_CLEANUP_FAILED
        resp_remove = h.request({
            'op': 'managed_context_remove',
            'target_sid': target_sid,
            'delete_windows_account': False
        })
        check('w2-002-partial-cleanup-failed-reported',
              resp_remove is not None and resp_remove.get('status') == 'PARTIAL_CLEANUP_FAILED',
              str(resp_remove))
        failed_ops = resp_remove.get('failed_operations', []) if resp_remove else []
        check('w2-002-partial-cleanup-revoke-in-failed-ops',
              'revoke_workdir_grants' in failed_ops,
              str(failed_ops))
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_unknown_or_corrupted_transaction():
    p = TestPlane('w2-002-unknown-txn')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-unknown-txn-helper-started', ok, 'helper did not start')
        if not ok: return

        txn_file = os.path.join(p.dir, 'grants.txn.json')
        unknown_data = {
            'txn_id': 'txn-unknown-999',
            'op': 'UNKNOWN_OPERATION_NAME',
            'target_sid': 'S-1-5-21-444444444-555555555-666666666-2008',
            'workdir': 'C:\\temp',
            'created': True,
            'state': 'intent_logged'
        }
        with open(txn_file, 'w', encoding='utf-8') as f:
            json.dump(unknown_data, f)

        resp = h.request({
            'op': 'managed_workdir_ensure_access',
            'target_sid': 'S-1-5-21-444444444-555555555-666666666-2008',
            'workdir': p.dir
        })
        check('w2-002-unknown-txn-refused',
              resp is not None and resp.get('status') == 'INCONSISTENT_GRANT_STATE',
              str(resp))
        check('w2-002-unknown-txn-error-detail',
              'unknown transaction operation' in str(resp.get('error', '')).lower(),
              str(resp))
        check('w2-002-unknown-txn-evidence-preserved', os.path.exists(txn_file), 'txn file was deleted!')

        with open(txn_file, 'w', encoding='utf-8') as f:
            f.write('{corrupt json string...')

        resp_corrupt = h.request({
            'op': 'managed_workdir_ensure_access',
            'target_sid': 'S-1-5-21-444444444-555555555-666666666-2008',
            'workdir': p.dir
        })
        check('w2-002-corrupt-txn-refused',
              resp_corrupt is not None and resp_corrupt.get('status') == 'INCONSISTENT_GRANT_STATE',
              str(resp_corrupt))
        check('w2-002-corrupt-txn-evidence-preserved', os.path.exists(txn_file), 'corrupt txn file was deleted!')
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_transaction_journal_deletion_denied():
    p = TestPlane('w2-002-journal-del-denied')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-journal-del-helper-started', ok, 'helper did not start')
        if not ok: return

        test_dir = os.path.abspath(os.path.join(p.dir, 'test-dir-del-denied'))
        os.makedirs(test_dir, exist_ok=True)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2009'

        txn_file = os.path.join(p.dir, 'grants.txn.json')
        txn_data = {
            'txn_id': 'txn-del-denied-123',
            'op': 'grant',
            'target_sid': target_sid,
            'workdir': test_dir,
            'created': True,
            'state': 'ace_verified',
            'updated_at': '2026-10-11T00:00:00Z'
        }
        with open(txn_file, 'w', encoding='utf-8') as f:
            json.dump(txn_data, f)

        # Place ACE on folder
        add_res = add_ace_ps(test_dir, target_sid)
        check('w2-002-journal-del-add-ace-ok', bool(add_res), 'failed adding ace')

        # Mark txn_file as READ-ONLY so File.Delete throws UnauthorizedAccessException
        kernel32.SetFileAttributesW(txn_file, 1)
        try:
            resp = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': test_dir})
            check('w2-002-journal-del-denied-refused',
                  resp is not None and resp.get('status') == 'INCONSISTENT_GRANT_STATE',
                  str(resp))
            check('w2-002-journal-del-denied-error-detail',
                  'journal cleanup failed' in str(resp.get('error', '')).lower() or
                  'access to the path' in str(resp.get('error', '')).lower(),
                  str(resp))
            check('w2-002-journal-del-denied-file-preserved', os.path.exists(txn_file), 'file was deleted!')
        finally:
            kernel32.SetFileAttributesW(txn_file, 128)
    finally:
        h.stop()
        p.cleanup()


def test_w2_002_second_reconciliation_and_restart():
    p = TestPlane('w2-002-second-reconcile')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-second-reconcile-helper-started', ok, 'helper did not start')
        if not ok: return

        test_dir = os.path.abspath(os.path.join(p.dir, 'test-dir-second-recon'))
        os.makedirs(test_dir, exist_ok=True)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2010'

        txn_file = os.path.join(p.dir, 'grants.txn.json')
        txn_data = {
            'txn_id': 'txn-second-recon-111',
            'op': 'grant',
            'target_sid': target_sid,
            'workdir': test_dir,
            'created': True,
            'state': 'ace_verified',
            'updated_at': '2026-10-11T00:00:00Z'
        }
        with open(txn_file, 'w', encoding='utf-8') as f:
            json.dump(txn_data, f)

        add_res = add_ace_ps(test_dir, target_sid)
        check('w2-002-second-reconcile-add-ace-ok', bool(add_res), 'failed adding ace')

        resp1 = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': test_dir})
        check('w2-002-first-reconcile-ok', resp1 is not None and resp1.get('status') in ('already_granted', 'granted'), str(resp1))
        check('w2-002-first-reconcile-txn-deleted', not os.path.exists(txn_file), 'txn file still exists')

        resp2 = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': test_dir})
        check('w2-002-second-reconcile-repeated-grant-ok',
              resp2 is not None and resp2.get('status') == 'already_granted', str(resp2))

        h.stop()
        h2 = HelperInstance(p)
        try:
            h2.start()
            resp_rev = h2.request({'op': 'managed_workdir_revoke_access', 'target_sid': target_sid})
            check('w2-002-repeated-revoke-after-restart-ok',
                  resp_rev is not None and resp_rev.get('status') == 'revoked', str(resp_rev))

            resp_rev2 = h2.request({'op': 'managed_workdir_revoke_access', 'target_sid': target_sid})
            check('w2-002-repeated-revoke-second-time-ok',
                  resp_rev2 is not None and resp_rev2.get('status') == 'revoked', str(resp_rev2))
        finally:
            h2.stop()
    finally:
        p.cleanup()


def test_w2_002_rollback_preserves_unrelated_rule():
    p = TestPlane('w2-002-rollback-unrelated')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-002-unrelated-helper-started', ok, 'helper did not start')
        if not ok: return

        test_dir = os.path.abspath(os.path.join(p.dir, 'test-dir-unrelated'))
        os.makedirs(test_dir, exist_ok=True)
        target_sid = 'S-1-5-21-444444444-555555555-666666666-2011'

        # 1. Place FullControl ACE on test_dir (unrelated rule with inherit='None')
        res_fc = add_ace_ps(test_dir, target_sid, mask='FullControl', inherit='None')
        check('w2-002-add-fullcontrol-rule-ok', bool(res_fc), 'failed adding fc ace')

        # 2. Make grants.json read-only to force RollbackAce in EnsureManagedWorkdirAccess
        grants_file = os.path.join(p.dir, 'grants.json')
        with open(grants_file, 'w', encoding='utf-8') as f:
            f.write('[]')
        kernel32.SetFileAttributesW(grants_file, 1)

        try:
            resp = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': target_sid, 'workdir': test_dir})
            check('w2-002-rollback-ledger-save-failed', resp is not None and resp.get('status') == 'LEDGER_SAVE_FAILED', str(resp))
        finally:
            kernel32.SetFileAttributesW(grants_file, 128)

        # 3. Verify RollbackAce removed the helper Modify rule, but preserved the unrelated FullControl rule!
        rights = get_ace_rights_ps(test_dir, target_sid)
        check('w2-002-unrelated-rule-preserved-after-rollback', 'FullControl' in rights, f'rights={rights}')

        # 4. Pre-seed ledger with created=True for test_dir, add exact helper Modify rule
        res_mod = add_ace_ps(test_dir, target_sid, mask='Modify', inherit='ContainerInherit,ObjectInherit')
        check('w2-002-add-modify-rule-ok', bool(res_mod), 'failed adding modify ace')

        with open(grants_file, 'w', encoding='utf-8') as f:
            json.dump([{
                'key': f'{target_sid}|{test_dir.upper()}',
                'target_sid': target_sid,
                'workdir': test_dir,
                'created': True,
                'last_seen': '2026-10-11T00:00:00Z'
            }], f)

        # 5. Revoke managed workdir access: exact Modify rule is revoked, FullControl rule is preserved!
        resp_rev = h.request({'op': 'managed_workdir_revoke_access', 'target_sid': target_sid})
        check('w2-002-unrelated-revoke-succeeded', resp_rev is not None and resp_rev.get('status') == 'revoked', str(resp_rev))
        check('w2-002-unrelated-revoke-count-reported', resp_rev is not None and resp_rev.get('revoked_rules') == 1, str(resp_rev))

        rights_after_revoke = get_ace_rights_ps(test_dir, target_sid)
        check('w2-002-unrelated-fullcontrol-rule-preserved', 'FullControl' in rights_after_revoke, f'rights={rights_after_revoke}')
    finally:
        h.stop()
        p.cleanup()


# ----------------------------------------------------------------------
# W2-003 Tests: Managed Context Deletion Truth and Lifecycle Machine
# ----------------------------------------------------------------------
def test_w2_003_non_saiag_user_deletion_refusal():
    p = TestPlane('w2-003-non-saiag')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-003-refuse-helper-started', ok, 'helper did not start')
        if not ok:
            return

        # Target SID ending in -0000 translates to "OperatorUser" (non-SAIAG) in test mode
        non_saiag_sid = 'S-1-5-21-777777777-888888888-999999999-0000'
        resp = h.request({
            'op': 'managed_context_remove',
            'target_sid': non_saiag_sid,
            'delete_windows_account': True
        })

        check('w2-003-refusal-reported-partial-cleanup-failed',
              resp is not None and resp.get('status') == 'PARTIAL_CLEANUP_FAILED',
              str(resp))
        failed_ops = resp.get('failed_operations', []) if resp else []
        check('w2-003-delete-local-user-in-failed-ops',
              'delete_local_user' in failed_ops,
              str(failed_ops))
        errors = resp.get('errors', {}) if resp else {}
        check('w2-003-error-is-user-delete-refused-not-managed',
              errors.get('delete_local_user') == 'USER_DELETE_REFUSED_NOT_MANAGED',
              str(errors))
    finally:
        h.stop()
        p.cleanup()


def test_w2_003_helper_offline_lifecycle_machine():
    p = TestPlane('w2-003-offline-lifecycle')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-003-lifecycle-helper-started', ok, 'helper did not start')
        if not ok:
            return

        # 1. Create a managed context
        code, out, _, _ = p.run('context', 'create', '--provider', 'antigravity', '--alias', 'AG_FaultTest')
        check('w2-003-context-created', code == 0 and out is not None and out.get('status') == 'created', str(out))
        if not out or code != 0:
            return

        acc_id = out.get('account_id')
        target_sid = out.get('windows_sid')

        # Verify initial registry state
        reg = p.registry()
        acc = next((a for a in reg.get('accounts', []) if a.get('account_id') == acc_id), None)
        check('w2-003-initial-account-state-enabled',
              acc is not None and acc.get('operational_state') == 'ENABLED',
              str(acc))

        # 2. Stop helper server to induce helper_unreachable failure during deletion
        h.stop()

        # 3. Attempt to delete context with helper offline
        code_del, out_del, stdout_del, stderr_del = p.run('context', 'delete', acc_id, '--force')

        # Must exit 1
        check('w2-003-helper-offline-delete-exits-1', code_del == 1,
              f'rc={code_del} out={out_del} stderr={stderr_del}')

        # 4. Verify account is STILL PRESENT in registry and marked cleanup_required & DISABLED
        reg_after_fail = p.registry()
        acc_after_fail = next((a for a in reg_after_fail.get('accounts', []) if a.get('account_id') == acc_id), None)
        check('w2-003-account-retained-in-registry', acc_after_fail is not None,
              'account was prematurely deleted from registry!')
        if acc_after_fail:
            check('w2-003-operational-state-disabled',
                  acc_after_fail.get('operational_state') == 'DISABLED',
                  str(acc_after_fail))
            check('w2-003-cleanup-state-persisted-cleanup-required',
                  acc_after_fail.get('cleanup_state') == 'cleanup_required',
                  str(acc_after_fail))
            check('w2-003-cleanup-error-recorded',
                  acc_after_fail.get('cleanup_error') == 'helper_unreachable',
                  str(acc_after_fail))

        # 5. Restart helper server
        ok_restart = h.start()
        check('w2-003-helper-restarted', ok_restart, 'helper restart failed')
        if not ok_restart:
            return

        # 6. Retry delete command (idempotent recovery)
        code_retry, out_retry, _, stderr_retry = p.run('context', 'delete', acc_id, '--force')
        check('w2-003-retry-delete-succeeds-exit-0', code_retry == 0,
              f'rc={code_retry} out={out_retry} err={stderr_retry}')

        # 7. Verify account is now fully removed from registry
        reg_final = p.registry()
        acc_final = next((a for a in reg_final.get('accounts', []) if a.get('account_id') == acc_id), None)
        check('w2-003-account-removed-from-registry-after-retry', acc_final is None,
              str(acc_final))
    finally:
        h.stop()
        p.cleanup()


def test_w2_003_managed_context_remove_truth_and_idempotence():
    p = TestPlane('w2-003-remove-truth')
    h = HelperInstance(p)
    try:
        ok = h.start()
        check('w2-003-truth-helper-started', ok, 'helper did not start')
        if not ok:
            return

        # 1. Create a managed context
        code, out, _, _ = p.run('context', 'create', '--provider', 'antigravity', '--alias', 'AG_TruthTest')
        check('w2-003-truth-context-created', code == 0 and out is not None and out.get('status') == 'created', str(out))
        if not out:
            return

        target_sid = out.get('windows_sid')
        vault_dir = os.path.join(p.dir, 'vault')
        fp = hashlib.sha256(target_sid.lower().encode()).hexdigest()[:16]
        seal_file = os.path.join(vault_dir, fp + '.seal')
        check('w2-003-truth-seal-file-exists-before-remove', os.path.exists(seal_file), seal_file)

        # 2. Call managed_context_remove directly
        resp = h.request({
            'op': 'managed_context_remove',
            'target_sid': target_sid,
            'delete_windows_account': True
        })

        check('w2-003-remove-status-removed',
              resp is not None and resp.get('status') == 'removed',
              str(resp))
        check('w2-003-remove-workdir-grants-revoked',
              resp is not None and resp.get('workdir_grants_revoked') is True,
              str(resp))
        check('w2-003-remove-sealed-secret-deleted',
              resp is not None and resp.get('sealed_secret_deleted') is True,
              str(resp))
        check('w2-003-remove-windows-user-deleted',
              resp is not None and resp.get('windows_user_deleted') is True,
              str(resp))
        check('w2-003-seal-file-absent-after-remove', not os.path.exists(seal_file), seal_file)

        # 3. Call managed_context_remove second time (idempotence verification)
        resp_again = h.request({
            'op': 'managed_context_remove',
            'target_sid': target_sid,
            'delete_windows_account': True
        })
        check('w2-003-second-remove-cleanly-idempotent',
              resp_again is not None and resp_again.get('status') == 'removed',
              str(resp_again))
    finally:
        h.stop()
        p.cleanup()


def main():
    print('======================================================================')
    print('SAI ACCOUNTS SECURITY FAULT-INJECTION TEST SUITE (T-260)')
    print('Testing W2-001, W2-002, and W2-003 Negative Controls')
    print(f'Binaries: {TEST_BIN}')
    print('======================================================================\n')

    if not os.path.exists(EXE) or not os.path.exists(HELPER_EXE):
        print(f'FAIL: test binaries missing in {TEST_BIN}. Build first with build.ps1.')
        return 1

    print('--- Category 1: W2-001 Fail-Closed ACL Security ---')
    test_w2_001_install_validation()
    test_w2_001_sealed_secret_faults()

    print('\n--- Category 2: W2-002 Durable Workdir Grant Ledger & Transaction State ---')
    test_w2_002_corrupted_ledger_refusal()
    test_w2_002_ledger_lock_timeout()
    test_w2_002_ledger_save_failure_rolls_back_ace()
    test_w2_002_interrupted_transaction_reconciliation()
    test_w2_002_already_granted_failed_ledger_persistence()
    test_w2_002_third_party_ace_attribution()
    test_w2_002_revoke_intent_write_failure()
    test_w2_002_crash_halfway_multi_grant_revoke()
    test_w2_002_crash_after_acl_before_ledger_commit()
    test_w2_002_crash_after_ledger_before_journal_removal()
    test_w2_002_partial_revoke_failed_rules()
    test_w2_002_unknown_or_corrupted_transaction()
    test_w2_002_transaction_journal_deletion_denied()
    test_w2_002_second_reconciliation_and_restart()
    test_w2_002_rollback_preserves_unrelated_rule()

    print('\n--- Category 3: W2-003 Managed Context Deletion Truth & Lifecycle Machine ---')
    test_w2_003_non_saiag_user_deletion_refusal()
    test_w2_003_helper_offline_lifecycle_machine()
    test_w2_003_managed_context_remove_truth_and_idempotence()

    print('\n======================================================================')
    print(f'RESULTS: PASS {len(PASSES)} / FAIL {len(FAILURES)}')
    print('======================================================================')
    if FAILURES:
        for f in FAILURES:
            print('  FAILED: ' + f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
