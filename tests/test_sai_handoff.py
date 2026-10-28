#!/usr/bin/env python3
"""SAI Accounts File Handoff (SAIHANDOFF) security regression suite.

Covers the complete SAIHANDOFF contract:
- Argument shape validation and fail-closed security
- Staging directory creation and .gitignore protection
- Disabling NTFS DACL inheritance and strict 3-ACE restriction:
  (Operator FullControl, SYSTEM FullControl, Target SID Modify|Synchronize)
  No foreign accounts, no Users group.
- Atomic file copy, byte length verification, and SHA-256 verification
- Stale partial write cleanup
- Broker file_probe oracle validation:
  - Rejection of commands/scripts/exe/programs/binaries/args/content
  - Readability, length, and sha256 reporting under broker token
- CLI verify-file command validation
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.sep + '..')
TEST_BIN = os.environ.get('SAI_ACCOUNTS_TEST_BIN') or os.path.join(ROOT, 'Scripts', 'accounts', 'bin')
EXE = os.path.join(TEST_BIN, 'sai-accounts.exe')

sys.path.insert(0, os.path.join(ROOT, 'tests'))
from test_sai_accounts import Plane, Broker, pipe_request

FAILURES = []
PASSES = []


def check(name, ok, detail=''):
    if ok:
        PASSES.append(name)
        print('PASS  ' + name)
    else:
        FAILURES.append(name + (' :: ' + detail if detail else ''))
        print('FAIL  ' + name + (' :: ' + detail if detail else ''))


PLANE_ENV = None


def run_cmd(args):
    """Run the CLI with the isolated registry environment.

    Without an explicit env the exe inherits os.environ, so its default registry
    root is the operator's LIVE one: the suite resolves the real AG2 account
    (including its credential metadata) and therefore only passes on a desk that
    happens to have that account. Every call is driven through the plane env.
    """
    env = PLANE_ENV if PLANE_ENV is not None else os.environ
    r = subprocess.run([EXE] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, env=env, timeout=120)
    out = r.stdout.strip()
    data = None
    if out:
        try:
            data = json.loads(out)
        except Exception:
            pass
    return r.returncode, data, out, r.stderr.strip()


def plane_accounts(plane):
    """Read the isolated registry through its own list verb.

    compact_label is computed at query time and is not stored on the row, so the
    registry file alone cannot answer which account is AG2."""
    doc = json.loads(subprocess.run([EXE, 'list'], capture_output=True, text=True,
                                    env=plane.env, timeout=120, cwd=plane.dir)
                     .stdout.strip().splitlines()[-1])
    return doc.get('accounts', [])


def plane_account(plane, label):
    """Read one synthetic account out of the isolated registry."""
    for row in plane_accounts(plane):
        if row.get('compact_label') == label:
            return {'windows_user': (row.get('provider_metadata') or {}).get('windows_user'),
                    'windows_sid': row.get('windows_sid'),
                    'account_id': row.get('account_id'),
                    'provider_id': row.get('provider_id')}
    raise KeyError('account %s is not in the isolated registry' % label)


def ag2_account(plane):
    return plane_account(plane, 'AG2')


def seed_plane(plane):
    """Populate the isolated registry with two synthetic Antigravity accounts.

    The suite selects AG2, so the plane must hold an AG2 -- otherwise every
    positive check answers ACCOUNT_NOT_FOUND and the suite measures nothing. The
    import path is the one test_sai_accounts.py's own registry-plane test uses,
    and the SIDs are synthetic: no Windows account is consulted.
    """
    from test_sai_accounts import ag_account
    plane.legacy('user', [ag_account('S-1-5-21-4000000000-4000000000-4000000000-1002', 'synthetic_op'),
                          ag_account('S-1-5-21-4000000000-4000000000-4000000000-1008', 'synthetic_ag_b')])
    proc = subprocess.run([EXE, 'import'], capture_output=True, text=True,
                          env=plane.env, timeout=120, cwd=plane.dir)
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'status': 'IMPORT_FAILED', 'stdout': proc.stdout[-300:], 'stderr': proc.stderr[-300:]}


def main():
    global PLANE_ENV
    print("Running SAI Accounts File Handoff security suite...")

    # The isolated registry comes first: every run_cmd below must speak to it,
    # never to the operator's live accounts root.
    plane = Plane()
    PLANE_ENV = plane.env
    seeded = seed_plane(plane)
    labels = [a.get('compact_label') for a in plane_accounts(plane)]
    check('plane-seeded-with-AG2', 'AG2' in labels, str(seeded)[:300])

    # 1. Argument validation
    rc, d, out, err = run_cmd(['handoff-file'])
    check('handoff-missing-selector-fails', rc != 0 and d and d.get('status') == 'missing_selector')

    rc, d, out, err = run_cmd(['handoff-file', 'AG2'])
    check('handoff-missing-source-fails', rc != 0 and d and d.get('status') == 'handoff_file_args')

    rc, d, out, err = run_cmd(['handoff-file', 'NONEXISTENT_ACCOUNT', '--source', 'foo.txt'])
    check('handoff-unknown-account-fails', rc != 0 and d and d.get('status') == 'ACCOUNT_NOT_FOUND')

    rc, d, out, err = run_cmd(['verify-file', 'AG2'])
    check('verify-missing-path-fails', rc != 0 and d and d.get('status') == 'verify_file_args')

    temp_dir = tempfile.mkdtemp(prefix='sai_handoff_test_')
    try:
        workdir = os.path.join(temp_dir, 'mock_workdir')
        os.makedirs(workdir, exist_ok=True)

        source_file = os.path.join(temp_dir, 'sample_image.png')
        test_payload = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' + b'mock_test_image_bytes_42'
        with open(source_file, 'wb') as f:
            f.write(test_payload)
        expected_sha = hashlib.sha256(test_payload).hexdigest()
        expected_len = len(test_payload)

        # 2. Source not found
        rc, d, out, err = run_cmd(['handoff-file', 'AG2', '--source', os.path.join(temp_dir, 'no_such_file.png'), '--workdir', workdir])
        check('handoff-source-not-found', rc != 0 and d and d.get('status') == 'SOURCE_NOT_FOUND')

        # 3. Source is directory
        sub_dir = os.path.join(temp_dir, 'mock_subdir')
        os.makedirs(sub_dir, exist_ok=True)
        rc, d, out, err = run_cmd(['handoff-file', 'AG2', '--source', sub_dir, '--workdir', workdir])
        check('handoff-source-is-directory-rejected', rc != 0 and d and d.get('status') == 'SOURCE_IS_DIRECTORY')

        # 4. Workdir not found
        rc, d, out, err = run_cmd(['handoff-file', 'AG2', '--source', source_file, '--workdir', os.path.join(temp_dir, 'nonexistent_workdir')])
        check('handoff-workdir-not-found', rc != 0 and d and d.get('status') == 'WORKDIR_NOT_FOUND')

        # 5. Successful handoff
        rc, d, out, err = run_cmd(['handoff-file', 'AG2', '--source', source_file, '--workdir', workdir])
        check('handoff-success-exit-0', rc == 0)
        check('handoff-schema-valid', d and d.get('schema') == 'sai.accounts/handoff-file/1')
        check('handoff-status-ok', d and d.get('status') == 'ok')
        check('handoff-length-match', d and d.get('length') == expected_len)
        check('handoff-sha256-match', d and d.get('sha256') == expected_sha)

        staged_path = d.get('staged_path') if d else ''
        check('handoff-staged-path-returned', bool(staged_path and os.path.exists(staged_path)))

        # Verify staged content matches source
        if staged_path and os.path.exists(staged_path):
            with open(staged_path, 'rb') as f:
                staged_bytes = f.read()
            check('handoff-content-identical', staged_bytes == test_payload)

        # 6. Verify .gitignore was created
        gitignore_path = os.path.join(workdir, '.sai-handoff', '.gitignore')
        check('handoff-gitignore-created', os.path.isfile(gitignore_path))
        if os.path.isfile(gitignore_path):
            with open(gitignore_path, 'r', encoding='utf-8') as f:
                gi_content = f.read()
            check('handoff-gitignore-content-blocks-all', '*\n!.gitignore\n' in gi_content or '*\r\n!.gitignore\r\n' in gi_content)

        # 7. Verify staging directory isolation: subfolder is .sai-handoff\ag2
        expected_staging_dir = os.path.join(workdir, '.sai-handoff', 'ag2')
        check('handoff-staging-dir-is-account-isolated', os.path.isdir(expected_staging_dir))

        # 8. Verify NTFS ACL via icacls
        icacls_out = subprocess.check_output(['icacls', expected_staging_dir], text=True)
        # Check inheritance is disabled: no (I) flags anywhere in output
        check('handoff-dacl-inheritance-disabled', '(I)' not in icacls_out)
        # Check the plane's own AG2 is granted. Its windows_user and SID are read
        # from the isolated registry: the operator's real account identifiers
        # must never appear in this file, because they would not be present on a
        # machine that has no such account.
        ag2 = ag2_account(plane)
        check('handoff-dacl-grants-target-account',
              ag2['windows_user'] in icacls_out or ag2['windows_sid'] in icacls_out,
              str(ag2))
        # Check the plane's other account is NOT in the DACL
        ag1 = plane_account(plane, 'AG1')
        check('handoff-dacl-foreign-account-excluded',
              ag1['windows_user'] not in icacls_out and ag1['windows_sid'] not in icacls_out,
              str(ag1))
        # Check unprivileged Users group is NOT in DACL
        check('handoff-dacl-users-group-excluded', 'BUILTIN\\Users' not in icacls_out and 'Authenticated Users' not in icacls_out)

        # 9. Verify idempotent overwrite with integrity
        test_payload2 = test_payload + b'_updated_revision_2'
        with open(source_file, 'wb') as f:
            f.write(test_payload2)
        expected_sha2 = hashlib.sha256(test_payload2).hexdigest()
        rc, d2, out, err = run_cmd(['handoff-file', 'AG2', '--source', source_file, '--workdir', workdir])
        check('handoff-overwrite-success', rc == 0 and d2.get('status') == 'ok')
        check('handoff-overwrite-sha256-updated', d2.get('sha256') == expected_sha2)
        with open(staged_path, 'rb') as f:
            check('handoff-staged-file-updated', f.read() == test_payload2)

        # 10. Verify partial file cleanup
        stale_file = os.path.join(expected_staging_dir, 'sample.partial.old.tmp')
        with open(stale_file, 'w') as f:
            f.write('stale')
        two_hours_ago = os.path.getmtime(stale_file) - 7200
        os.utime(stale_file, (two_hours_ago, two_hours_ago))
        run_cmd(['handoff-file', 'AG2', '--source', source_file, '--workdir', workdir])
        check('handoff-cleans-stale-partial-files', not os.path.exists(stale_file))

        # 11. Synthetic Broker file_probe testing
        plane = Plane()
        broker = Broker(plane)
        broker.start()
        pipe = broker.pipe_name()
        try:
            # Wait for broker online
            online = False
            for _ in range(50):
                try:
                    resp = pipe_request(pipe, {'cmd': 'status'}, timeout_s=1.0)
                    if resp and resp.get('status') == 'ok':
                        online = True
                        break
                except Exception:
                    time.sleep(0.1)
            check('test-broker-online', online)

            if online:
                # Banned fields test
                banned_fields = ["exe", "command", "command_line", "program", "args", "binary", "script", "content"]
                for bf in banned_fields:
                    probe_req = {'cmd': 'file_probe', 'path': source_file, bf: 'malicious_input'}
                    resp = pipe_request(pipe, probe_req, timeout_s=3.0)
                    check('broker-file-probe-rejects-' + bf,
                          resp and resp.get('status') == 'OPERATION_NOT_ALLOWED' and resp.get('rejected_field') == bf)

                # Missing path
                resp = pipe_request(pipe, {'cmd': 'file_probe'}, timeout_s=3.0)
                check('broker-file-probe-path-required', resp and resp.get('status') == 'PATH_REQUIRED')

                # Nonexistent file
                resp = pipe_request(pipe, {'cmd': 'file_probe', 'path': os.path.join(temp_dir, 'phantom.png')}, timeout_s=3.0)
                check('broker-file-probe-file-not-found', resp and resp.get('status') == 'FILE_NOT_FOUND')

                # Valid file probe
                resp = pipe_request(pipe, {'cmd': 'file_probe', 'path': source_file}, timeout_s=3.0)
                check('broker-file-probe-reads-valid-file',
                      resp and resp.get('status') == 'ok' and resp.get('readable') is True
                      and resp.get('length') == len(test_payload2)
                      and resp.get('sha256') == expected_sha2)
        finally:
            broker.stop()
            plane.cleanup()

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("\nRESULTS: %d passed, %d failed" % (len(PASSES), len(FAILURES)))
    if FAILURES:
        for f in FAILURES:
            print("  FAIL: " + f)
        sys.exit(1)
    else:
        print("ALL HANDOFF TESTS PASSED.")
        sys.exit(0)


if __name__ == '__main__':
    main()
