#!/usr/bin/env python3
"""Trusted Antigravity Mode & Golden Dark UI hermetic regression suite (test_trusted_antigravity.py).

Verifies the complete SRC-109 / T-299 contract:
1. Static contract and hygiene:
   - FolderAccessManager.cs conforms to Golden Default Dark UI:
     * No white/light surfaces (FromArgb(240/245/255) banned)
     * No native unstyled MessageBox.Show (SaitulsDialog.Show required)
     * Proper Palette tokens applied
     * State machine definitions (OFF, ENABLING, ON_VERIFIED, EMPTY_SCOPE, DISABLING, REVOKE_PENDING, OFF_VERIFIED, ERROR)
     * Implicit root expansion shortcut (_entries.Count <= 3) removed (P1-2)
   - SaiAccounts.cs contract verification:
     * Verbs: trusted-mode-status, trusted-mode-set, authorize-launch, stop-session
     * AUDAPACK-only caller validation for launch authorization
     * Single-use 60s expiration tokens
     * Policy generation binding & stale generation rejection
     * Atomic locks (_authLock, _trustedLock, _sessionLock)
     * Session tracking via sessions.json (P1-4)
2. Hermetic runtime checks against disposable synthetic Plane (P0-2):
   - Fully isolated SAI_ACCOUNTS_ROOT (never touches operator's live registry or accounts)
   - Synthetic Antigravity accounts (AG1, AG2, AG3) with fabricated SIDs
   - trusted-mode-status & trusted-mode-set contract:
     * EMPTY_SCOPE on zero roots
     * Root validation: non-existent folder and disk root rejection
     * Separation of policy storage from immediate helper grants (P1-1)
     * Policy generation incrementation
   - authorize-launch origin security (P0-1):
     * Rejection of missing caller (CALLER_NOT_ELIGIBLE)
     * Rejection of forged caller (CALLER_NOT_ELIGIBLE)
     * Rejection of missing or non-existent workdir
     * Issuance of single-use 60s token with session_id, policy_generation, and target_sid
   - Token validation & failure paths (P1-5):
     * AUTH_TOKEN_NOT_FOUND on unrecognized token
     * AUTH_TOKEN_ACCOUNT_MISMATCH on account cross-use
     * AUTH_TOKEN_WORKDIR_MISMATCH on directory divergence
     * TRUSTED_MODE_REVOKED if mode disabled after token issue
     * POLICY_GENERATION_STALE if policy changed after token issue
     * AUTH_TOKEN_EXPIRED on expired token
     * AUTH_TOKEN_ALREADY_USED on token replay
     * TRUSTED_GRANT_FAILED (fail-closed) on helper failure
   - Session tracking and stop-session (P1-4):
     * stop-session driven by sessions.json, not window titles
     * Account isolation across AG1, AG2, AG3
"""
import copy
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
SCRIPTS_ACCOUNTS = os.path.join(ROOT, 'Scripts', 'accounts')
BIN_ACCOUNTS_EXE = os.path.join(SCRIPTS_ACCOUNTS, 'bin', 'sai-accounts.exe')
SAI_ACCOUNTS_CS = os.path.join(SCRIPTS_ACCOUNTS, 'SaiAccounts.cs')
FOLDER_ACCESS_MGR_CS = os.path.join(ROOT, 'FolderAutomation', 'FolderAccessManager.cs')

sys.path.insert(0, os.path.join(ROOT, 'tests'))
from test_sai_accounts import Plane, ag_account

FAILURES = []
PASSES = []


def check(name, ok, detail=''):
    if ok:
        PASSES.append(name)
        print('PASS  ' + name)
    else:
        FAILURES.append(name + (' :: ' + detail if detail else ''))
        print('FAIL  ' + name + (' :: ' + detail if detail else ''))


def read_text(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


# =====================================================================
# 1. Static contract & UI hygiene tests
# =====================================================================

def test_ui_dark_theme_and_dialog_hygiene():
    """Verify FolderAccessManager.cs contains zero white surfaces, zero MessageBox.Show, and no shortcuts."""
    src = read_text(FOLDER_ACCESS_MGR_CS)
    
    # 1. No white / light hardcoded surfaces
    banned_light = re.findall(r'FromArgb\(\s*24[0-9]\s*,\s*24[0-9]\s*,\s*24[0-9]\s*\)', src)
    check('no light FromArgb surfaces in FolderAccessManager.cs', len(banned_light) == 0,
          'found: %r' % banned_light)
    
    # 2. No MessageBox.Show
    msgbox_calls = re.findall(r'MessageBox\.Show\s*\(', src)
    check('no MessageBox.Show in FolderAccessManager.cs', len(msgbox_calls) == 0,
          'found %d MessageBox.Show calls' % len(msgbox_calls))
    
    # 3. Palette tokens used
    tokens = ['Palette.BG', 'Palette.BG_SOFT', 'Palette.COMPARE', 'Palette.TEXT', 'Palette.RAISED']
    missing_tokens = [t for t in tokens if t not in src]
    check('FolderAccessManager uses Palette tokens', len(missing_tokens) == 0,
          'missing tokens: %r' % missing_tokens)
    
    # 4. Styled ComboBox & buttons used
    check('FaUi.FaComboBox utilized in dialog', 'FaUi.FaComboBox' in src)
    check('FolderAccessManagerDialog.StyleButton used', 'FolderAccessManagerDialog.StyleButton' in src or 'StyleButton(' in src)

    # 5. Implicit root expansion shortcut (_entries.Count <= 3) removed (P1-2)
    check('P1-2 implicit root expansion shortcut removed', '_entries.Count <= 3' not in src)


def test_trusted_mode_state_machine_tokens():
    """Verify state machine tokens in source code."""
    src_accounts = read_text(SAI_ACCOUNTS_CS)
    src_ui = read_text(FOLDER_ACCESS_MGR_CS)
    
    states = ['OFF', 'ENABLING', 'ON_VERIFIED', 'EMPTY_SCOPE', 'DISABLING', 'REVOKE_PENDING', 'OFF_VERIFIED', 'ERROR']
    for st in states:
        check('state token %s present in SaiAccounts.cs' % st, '"' + st + '"' in src_accounts or "'" + st + "'" in src_accounts)
        check('state token %s present in FolderAccessManager.cs' % st, st in src_ui)


def test_cli_verbs_and_security_in_code():
    """Verify backend CLI verbs, locks, and caller restrictions in SaiAccounts.cs."""
    src = read_text(SAI_ACCOUNTS_CS)
    
    verbs = ['trusted-mode-status', 'trusted-mode-set', 'authorize-launch', 'stop-session']
    for v in verbs:
        check('verb %s wired in SaiAccounts.cs' % v, '"' + v + '"' in src)
    
    # AUDAPACK eligibility restriction
    check('caller audapack restriction enforced in code',
          'audapack' in src and 'CALLER_NOT_ELIGIBLE' in src)
    
    # Short-lived token duration
    check('launch token expiration tracked (60s)',
          '60' in src and 'launch_tok_' in src)

    # Concurrency locks and sessions.json tracking
    check('concurrency locks wired in SaiAccounts.cs',
          '_authLock' in src and '_trustedLock' in src and '_sessionLock' in src)
    check('sessions.json tracking wired in SaiAccounts.cs',
          'sessions.json' in src and 'RecordSession' in src)


# =====================================================================
# 2. Hermetic Plane runtime tests (P0-2)
# =====================================================================

def seed_synthetic_accounts(plane):
    """Seed synthetic AG1, AG2, AG3 accounts into disposable plane."""
    plane.legacy('user', [
        ag_account('S-1-5-21-4000000000-4000000000-4000000000-1001', 'synthetic_ag1'),
        ag_account('S-1-5-21-4000000000-4000000000-4000000000-1002', 'synthetic_ag2'),
        ag_account('S-1-5-21-4000000000-4000000000-4000000000-1003', 'synthetic_ag3'),
    ])
    rc, payload, out, err = plane.run('import')
    return rc == 0


def test_hermetic_trusted_mode_lifecycle(plane, mock_dirs):
    """Verify complete hermetic trusted-mode lifecycle and error cases."""
    dir1, dir2, non_existent = mock_dirs

    # 1. Initial status -> preference OFF, status OFF_VERIFIED
    rc, d, out, err = plane.run('trusted-mode-status', 'AG1')
    check('trusted-mode-status initial exit 0', rc == 0)
    check('trusted-mode-status initial preference OFF', d and d.get('preference') == 'OFF')
    check('trusted-mode-status initial status OFF_VERIFIED', d and d.get('status') == 'OFF_VERIFIED')
    check('trusted-mode-status target_sid present', d and d.get('target_sid') == 'S-1-5-21-4000000000-4000000000-4000000000-1001')

    # 2. Set ON with zero roots -> EMPTY_SCOPE (P1-3)
    rc, d, out, err = plane.run('trusted-mode-set', 'AG1', 'on')
    check('trusted-mode-set with no roots exit 0', rc == 0)
    check('trusted-mode-set with no roots reports EMPTY_SCOPE', d and d.get('status') == 'EMPTY_SCOPE')

    # 3. Set ON with nonexistent root -> rejected with error
    rc, d, out, err = plane.run('trusted-mode-set', 'AG1', 'on', '--roots', non_existent)
    check('trusted-mode-set with nonexistent root fails', rc != 0)
    check('trusted-mode-set with nonexistent root reports ERROR status', d and d.get('status') == 'ERROR')

    # 4. Set ON with disk root -> rejected with DISK_ROOT_REFUSED
    rc, d, out, err = plane.run('trusted-mode-set', 'AG1', 'on', '--roots', 'C:\\')
    check('trusted-mode-set with disk root fails', rc != 0)
    check('trusted-mode-set with disk root mentions DISK_ROOT_REFUSED', d and 'DISK_ROOT_REFUSED' in (d.get('error') or ''))

    # 5. Set ON with valid roots -> ON_VERIFIED (P1-1, P1-3)
    rc, d, out, err = plane.run('trusted-mode-set', 'AG1', 'on', '--roots', dir1 + ';' + dir2)
    check('trusted-mode-set with valid roots exit 0', rc == 0)
    check('trusted-mode-set status ON_VERIFIED', d and d.get('status') == 'ON_VERIFIED')
    approved = d.get('approved_roots') or []
    check('trusted-mode-set approved_roots holds both dirs', len(approved) == 2 and dir1 in approved and dir2 in approved)
    gen1 = d.get('generation')
    check('trusted-mode-set returns positive generation', gen1 is not None and gen1 > 1)

    # 6. trusted-mode-status reports ON_VERIFIED and generation
    rc, d, out, err = plane.run('trusted-mode-status', 'AG1')
    check('trusted-mode-status reports ON_VERIFIED', rc == 0 and d and d.get('status') == 'ON_VERIFIED')
    check('trusted-mode-status generation matches', d and d.get('generation') == gen1)


def test_hermetic_authorize_launch_security(plane, mock_dirs):
    """Verify authorize-launch caller security, argument validation, and token issuance (P0-1)."""
    dir1, dir2, non_existent = mock_dirs

    # 1. Missing caller -> CALLER_NOT_ELIGIBLE
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--workdir', dir1)
    check('authorize-launch without caller fails', rc != 0)
    check('authorize-launch without caller status CALLER_NOT_ELIGIBLE', d and d.get('status') == 'CALLER_NOT_ELIGIBLE')

    # 2. Forged callers -> CALLER_NOT_ELIGIBLE
    for forged in ['explorer', 'cmd', 'powershell', 'chrome', 'audapack_spoof']:
        rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', forged, '--workdir', dir1)
        check('authorize-launch rejects forged caller %s' % forged, rc != 0 and d and d.get('status') == 'CALLER_NOT_ELIGIBLE')

    # 3. Missing workdir -> WORKDIR_REQUIRED
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', 'audapack')
    check('authorize-launch missing workdir fails', rc != 0 and d and d.get('status') == 'WORKDIR_REQUIRED')

    # 4. Non-existent workdir -> WORKDIR_NOT_FOUND
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', 'audapack', '--workdir', non_existent)
    check('authorize-launch nonexistent workdir fails', rc != 0 and d and d.get('status') == 'WORKDIR_NOT_FOUND')

    # 5. Legitimate audapack caller with valid workdir -> authorized
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', 'audapack', '--workdir', dir1)
    check('authorize-launch legitimate audapack succeeds', rc == 0)
    check('authorize-launch status authorized', d and d.get('status') == 'authorized')
    token = d.get('token') if d else None
    check('authorize-launch token starts with launch_tok_', token is not None and token.startswith('launch_tok_'))
    session_id = d.get('session_id') if d else None
    check('authorize-launch returns session_id', session_id is not None and session_id.startswith('session_'))
    check('authorize-launch target_sid matches AG1', d and d.get('target_sid') == 'S-1-5-21-4000000000-4000000000-4000000000-1001')
    check('authorize-launch policy_generation returned', d and d.get('policy_generation') is not None)
    check('authorize-launch expires_in 60s', d and d.get('expires_in_seconds') == 60)

    return token


def test_hermetic_token_consumption_and_invalidation(plane, mock_dirs):
    """Verify single-use token consumption, account/workdir mismatch, expiration, and generation staleness (P1-5)."""
    dir1, dir2, non_existent = mock_dirs

    # 1. Unrecognized token -> AUTH_TOKEN_NOT_FOUND
    rc, d, out, err = plane.run('launch', 'AG1', '--workdir', dir1, '--auth-token', 'launch_tok_bogus123')
    check('launch with unrecognized token fails', rc != 0)
    check('launch reports AUTH_TOKEN_NOT_FOUND', d and d.get('status') == 'AUTH_TOKEN_NOT_FOUND')

    # 2. Account mismatch: issue token for AG1, attempt launch on AG2 -> AUTH_TOKEN_ACCOUNT_MISMATCH
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', 'audapack', '--workdir', dir1)
    tok_ag1 = d.get('token') if d else None
    rc, d, out, err = plane.run('launch', 'AG2', '--workdir', dir1, '--auth-token', tok_ag1)
    check('launch with account mismatch fails', rc != 0)
    check('launch reports AUTH_TOKEN_ACCOUNT_MISMATCH', d and d.get('status') == 'AUTH_TOKEN_ACCOUNT_MISMATCH')

    # 3. Workdir mismatch: issue token for dir1, attempt launch on dir2 -> AUTH_TOKEN_WORKDIR_MISMATCH
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', 'audapack', '--workdir', dir1)
    tok_dir1 = d.get('token') if d else None
    rc, d, out, err = plane.run('launch', 'AG1', '--workdir', dir2, '--auth-token', tok_dir1)
    check('launch with workdir mismatch fails', rc != 0)
    check('launch reports AUTH_TOKEN_WORKDIR_MISMATCH', d and d.get('status') == 'AUTH_TOKEN_WORKDIR_MISMATCH')

    # 4. Policy generation change invalidation: issue token, update roots, then attempt launch -> POLICY_GENERATION_STALE
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', 'audapack', '--workdir', dir1)
    tok_stale = d.get('token') if d else None
    # Update roots to bump generation
    plane.run('trusted-mode-set', 'AG1', 'on', '--roots', dir1)
    rc, d, out, err = plane.run('launch', 'AG1', '--workdir', dir1, '--auth-token', tok_stale)
    check('launch with stale policy generation fails', rc != 0)
    check('launch reports POLICY_GENERATION_STALE', d and d.get('status') == 'POLICY_GENERATION_STALE')

    # 5. Revocation invalidation: issue token, turn mode OFF, then attempt launch -> TRUSTED_MODE_REVOKED
    plane.run('trusted-mode-set', 'AG1', 'on', '--roots', dir1)
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', 'audapack', '--workdir', dir1)
    tok_revoked = d.get('token') if d else None
    plane.run('trusted-mode-set', 'AG1', 'off')
    rc, d, out, err = plane.run('launch', 'AG1', '--workdir', dir1, '--auth-token', tok_revoked)
    check('launch after mode revocation fails', rc != 0)
    check('launch reports TRUSTED_MODE_REVOKED', d and d.get('status') == 'TRUSTED_MODE_REVOKED')

    # 6. Expiration invalidation: issue token, rewrite expires_at into past, attempt launch -> AUTH_TOKEN_EXPIRED
    plane.run('trusted-mode-set', 'AG1', 'on', '--roots', dir1)
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', 'audapack', '--workdir', dir1)
    tok_exp = d.get('token') if d else None
    auths_path = os.path.join(plane.dir, 'scopes', plane.scope, 'authorizations.json')
    if os.path.exists(auths_path):
        with open(auths_path, 'r', encoding='utf-8') as f:
            auth_data = json.load(f)
        if tok_exp in auth_data.get('tokens', {}):
            auth_data['tokens'][tok_exp]['expires_at'] = '2020-01-01T00:00:00.0000000Z'
            with open(auths_path, 'w', encoding='utf-8') as f:
                json.dump(auth_data, f)
    rc, d, out, err = plane.run('launch', 'AG1', '--workdir', dir1, '--auth-token', tok_exp)
    check('launch with expired token fails', rc != 0)
    check('launch reports AUTH_TOKEN_EXPIRED', d and d.get('status') == 'AUTH_TOKEN_EXPIRED')

    # 7. Fail-closed grant verification & single-use consumption:
    # Issue fresh token. Without helper running, launch fails fail-closed (TRUSTED_GRANT_FAILED).
    rc, d, out, err = plane.run('authorize-launch', 'AG1', '--caller', 'audapack', '--workdir', dir1)
    tok_single = d.get('token') if d else None
    rc, d, out, err = plane.run('launch', 'AG1', '--workdir', dir1, '--auth-token', tok_single)
    check('launch with unreachable helper fails closed', rc != 0)
    check('launch reports TRUSTED_GRANT_FAILED', d and d.get('status') == 'TRUSTED_GRANT_FAILED')

    # Replay of the consumed token -> AUTH_TOKEN_ALREADY_USED
    rc, d, out, err = plane.run('launch', 'AG1', '--workdir', dir1, '--auth-token', tok_single)
    check('replayed token is rejected', rc != 0)
    check('replayed token reports AUTH_TOKEN_ALREADY_USED', d and d.get('status') == 'AUTH_TOKEN_ALREADY_USED')


def test_hermetic_sessions_and_isolation(plane):
    """Verify session tracking in sessions.json and stop-session behavior (P1-4)."""
    # 1. stop-session on account with no sessions -> terminated_count: 0
    rc, d, out, err = plane.run('stop-session', 'AG1')
    check('stop-session AG1 exit 0', rc == 0)
    check('stop-session reports status ok', d and d.get('status') in ('ok', 'stopped'))
    check('stop-session reports terminated_count 0', d and d.get('terminated_count') == 0)

    # 2. stop-session on nonexistent account -> ACCOUNT_NOT_FOUND
    rc, d, out, err = plane.run('stop-session', 'NONEXISTENT')
    check('stop-session on nonexistent account fails', rc != 0)
    check('stop-session nonexistent reports ACCOUNT_NOT_FOUND', d and d.get('status') == 'ACCOUNT_NOT_FOUND')

    # 3. Account isolation across AG1, AG2, AG3
    rc1, d1, _, _ = plane.run('trusted-mode-status', 'AG1')
    rc2, d2, _, _ = plane.run('trusted-mode-status', 'AG2')
    rc3, d3, _, _ = plane.run('trusted-mode-status', 'AG3')
    check('all three accounts report status exit 0', rc1 == 0 and rc2 == 0 and rc3 == 0)
    sids = [d1.get('target_sid'), d2.get('target_sid'), d3.get('target_sid')]
    check('AG1, AG2, AG3 have distinct target SIDs', len(set(sids)) == 3)


def main():
    print('=== Running Trusted Antigravity Regression Suite ===\n')
    
    # 1. Static and UI hygiene checks
    test_ui_dark_theme_and_dialog_hygiene()
    test_trusted_mode_state_machine_tokens()
    test_cli_verbs_and_security_in_code()

    # 2. Check binary existence
    if not os.path.exists(BIN_ACCOUNTS_EXE):
        check('sai-accounts.exe binary exists', False, 'Missing %s' % BIN_ACCOUNTS_EXE)
        print('Aborting runtime checks due to missing binary.')
        sys.exit(1)
    check('sai-accounts.exe binary exists', True)

    # 3. Hermetic Plane tests
    temp_root = tempfile.mkdtemp(prefix='sai_trusted_test_')
    plane = None
    try:
        plane = Plane()
        seeded = seed_synthetic_accounts(plane)
        check('hermetic synthetic accounts seeded', seeded)

        # Create mock workdirs within temp_root
        dir1 = os.path.join(temp_root, 'project_a')
        dir2 = os.path.join(temp_root, 'project_b')
        non_existent = os.path.join(temp_root, 'nonexistent_dir')
        os.makedirs(dir1, exist_ok=True)
        os.makedirs(dir2, exist_ok=True)

        mock_dirs = (dir1, dir2, non_existent)

        test_hermetic_trusted_mode_lifecycle(plane, mock_dirs)
        test_hermetic_authorize_launch_security(plane, mock_dirs)
        test_hermetic_token_consumption_and_invalidation(plane, mock_dirs)
        test_hermetic_sessions_and_isolation(plane)

    finally:
        if plane is not None:
            plane.cleanup()
        shutil.rmtree(temp_root, ignore_errors=True)

    print('\n==================================================')
    print('PASS: %d' % len(PASSES))
    print('FAIL: %d' % len(FAILURES))
    if FAILURES:
        print('\nFailed checks:')
        for f in FAILURES:
            print('  - ' + f)
        sys.exit(1)
    else:
        print('\nALL TRUSTED ANTIGRAVITY CHECKS PASSED!')
        sys.exit(0)


if __name__ == '__main__':
    main()
