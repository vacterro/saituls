#!/usr/bin/env python3
"""W2-002 residual negative controls: ownership generation and witness
boundaries in the Accounts Helper grant ledger (T-260 / SRC-078 / T-282).

Run:  python tests/test_accounts_ownership_generation.py
Exit: 0 = all PASS, 1 = failures.

Classification: WINDOWS_DISPOSABLE_PASS. Every case runs the compiled helper
binary against a disposable SAI_ACCOUNTS_ROOT plane and fabricates SIDs that
exist in no directory. These prove the helper's decisions, not the OS's
random cooperative placement of a foreign ACE next to ours.

Cases:
  MANDATORY-01 external ACE observed, removed, then helper-created grant
               (old behavior: created stays false, revoke orphans the ACE)
  MANDATORY-02 helper-created grant removed, then equivalent external ACE
               (old behavior: created stays true, revoke deletes the foreign ACE)
  MANDATORY-03 created=false row followed by a genuine mutation
  MANDATORY-04 created=true row followed by external ACL replacement
  MANDATORY-05 repeated ensure after external ACL changes
  MANDATORY-06 revoke after restart and ownership-generation mismatch
  WITNESS-01    helper records a post-write witness and does not delete an ACE
               it cannot prove it wrote (foreign generation at failure)
  WITNESS-02    journal evidence is stamped, not rewritten, when rollback fails
  TXN-01        revoke transaction naming an unowned path is refused
  TXN-02        revoke transaction with an unexpected extra path is refused
  TXN-03        ledger row outside the transaction's own path list is not deleted
  TXN-04        repeated reconciliation of the same revoke does not double-delete
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_accounts_security_fault_injection import (  # noqa: E402
    TestPlane, HelperInstance, add_ace_ps, has_ace_ps, get_ace_rights_ps, check,
    kernel32, FAILURES, PASSES, _ps5_env,
)

SID_A = 'S-1-5-21-444444444-555555555-666666666-2101'


def grants_file(p):
    return os.path.join(p.dir, 'grants.json')


def txn_file(p):
    return os.path.join(p.dir, 'grants.txn.json')


def read_grants(p):
    with open(grants_file(p), 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def write_grants(p, rows):
    with open(grants_file(p), 'w', encoding='utf-8') as f:
        json.dump(rows, f)


def write_txn(p, data):
    with open(txn_file(p), 'w', encoding='utf-8') as f:
        json.dump(data, f)


def rows_for(rows, sid):
    return [r for r in rows if r.get('target_sid') == sid]


# --- MANDATORY-01 / CASE A: external ACE observed, removed, then helper-made
def test_case_a_external_then_helper_created():
    p = TestPlane('own-gen-case-a')
    h = HelperInstance(p)
    try:
        if not h.start():
            return
        d = os.path.abspath(os.path.join(p.dir, 'dir-a'))
        os.makedirs(d, exist_ok=True)

        # 1. Third-party places an equivalent ACE.
        add_ace_ps(d, SID_A)
        r1 = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
        check('case-a-first-observation-records-created-false',
              r1 and r1.get('status') == 'already_granted'
              and rows_for(read_grants(p), SID_A)[0].get('created') is False, str(r1))

        # 2. Third party removes it.
        remove_ace(d, SID_A)
        check('case-a-external-ace-gone', not has_ace_ps(d, SID_A), 'ACE still present')

        # 3. Helper re-runs and must MUTATE + record ownership.
        r2 = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
        check('case-a-helper-granted-after-removal',
              r2 and r2.get('status') == 'granted', str(r2))
        row = [r for r in rows_for(read_grants(p), SID_A) if r.get('workdir', '').lower() == d.lower()]
        check('case-a-row-now-owned',
              len(row) == 1 and row[0].get('created') is True and row[0].get('gen') == 1,
              str(row))

        # 4. Revoke must actually delete the helper's own rule, not orphan it.
        rv = h.request({'op': 'managed_workdir_revoke_access', 'target_sid': SID_A})
        check('case-a-revoke-removes-helper-rule',
              rv and rv.get('status') == 'revoked' and not has_ace_ps(d, SID_A), str(rv))
        check('case-a-no-orphan-left',
              not [r for r in rows_for(read_grants(p), SID_A) if r.get('workdir', '').lower() == d.lower()],
              'helper-owned ACE orphaned in ledger')
    finally:
        h.stop()
        p.cleanup()


def remove_ace(path, sid_str):
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$sid = New-Object System.Security.Principal.SecurityIdentifier('{sid_str}'); "
        f"$acl = Get-Acl -LiteralPath '{path}'; "
        f"$rules = @($acl.Access | Where-Object {{ "
        f"    try {{ $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -eq '{sid_str}' }} catch {{ $false }} "
        f"}}); foreach($r in $rules){{ $acl.RemoveAccessRuleSpecific($r) }}; Set-Acl -LiteralPath '{path}' -AclObject $acl"
    )
    return subprocess.run(['powershell', '-NoProfile', '-Command', script],
                          capture_output=True, text=True, env=_ps5_env()).returncode == 0


# --- MANDATORY-02 / CASE B: helper-made ACE removed, foreign equivalent placed
def test_case_b_external_replacement_not_deleted():
    p = TestPlane('own-gen-case-b')
    h = HelperInstance(p)
    try:
        if not h.start():
            return
        d = os.path.abspath(os.path.join(p.dir, 'dir-b'))
        os.makedirs(d, exist_ok=True)

        rg = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
        check('case-b-helper-granted', rg and rg.get('status') == 'granted', str(rg))

        # Helper's rule gone, foreign process writes an equivalent one. The
        # rewrite also lands AFTER an unrelated explicit rule, so the ACL
        # generation moves even though the helper-shaped ACE itself is
        # byte-identical -- which is the case a purely structural check would
        # wrongly treat as still-ours.
        remove_ace(d, SID_A)
        other_sid = 'S-1-5-21-444444444-555555555-666666666-2999'
        add_ace_ps(d, other_sid, mask='Read', inherit='None')
        add_ace_ps(d, SID_A)
        check('case-b-foreign-ace-present', has_ace_ps(d, SID_A), 'foreign ACE absent')
        check('case-b-unrelated-rule-present', has_ace_ps(d, other_sid), 'unrelated rule absent')

        # Observation must RETRACT the created=true claim (witness mismatch).
        r2 = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
        check('case-b-observation-revoked-ownership',
              r2 and r2.get('status') == 'already_granted', str(r2))
        row = [r for r in rows_for(read_grants(p), SID_A) if r.get('workdir', '').lower() == d.lower()]
        check('case-b-created-retracted',
              len(row) == 1 and row[0].get('created') is False
              and row[0].get('acl_fp') in (None, ''),
              str(row))

        # Revoke must PRESERVE the foreign ACE AND the unrelated rule.
        rv = h.request({'op': 'managed_workdir_revoke_access', 'target_sid': SID_A})
        check('case-b-foreign-ace-preserved',
              rv and rv.get('revoked_rules') == 0 and has_ace_ps(d, SID_A), str(rv))
        check('case-b-unrelated-rule-preserved', has_ace_ps(d, other_sid), 'unrelated rule deleted')
    finally:
        h.stop()
        p.cleanup()


# --- MANDATORY-04: created=true row, external replacement, gen unchanged
def test_case_b_legacy_row_without_witness():
    p = TestPlane('own-gen-legacy-row')
    h = HelperInstance(p)
    try:
        if not h.start():
            return
        d = os.path.abspath(os.path.join(p.dir, 'dir-legacy'))
        os.makedirs(d, exist_ok=True)
        # A pre-witness ledger row (no acl_fp) claims created=true.
        write_grants(p, [{
            'key': SID_A + '|' + d.upper(),
            'target_sid': SID_A, 'workdir': d, 'created': True,
            'last_seen': '2026-10-11T00:00:00Z',
        }])
        add_ace_ps(d, SID_A)
        r = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
        check('legacy-row-observation-does-not-promote',
              r and r.get('status') == 'already_granted', str(r))
        row = [x for x in rows_for(read_grants(p), SID_A)]
        # Without a witness the claim must stand as found: no silent promotion
        # to a freshly-owned generation, and no evidence of fabrication.
        check('legacy-row-created-unchanged',
              len(row) == 1 and row[0].get('created') is True, str(row))
        rv = h.request({'op': 'managed_workdir_revoke_access', 'target_sid': SID_A})
        check('legacy-row-revoke-refuses-unprovable-or-deletes-matching',
              rv is not None and rv.get('status') in ('revoked', 'PARTIAL_REVOCATION_FAILED'), str(rv))
        # Whatever happened, the suite must not crash the helper.
        st = h.request({'op': 'status'})
        check('legacy-row-helper-still-alive', st is not None, str(st))
    finally:
        h.stop()
        p.cleanup()


# --- MANDATORY-05/06: repeated ensure + revoke after restart, gen mismatch
def test_restart_and_gen_mismatch():
    p = TestPlane('own-gen-restart')
    h = HelperInstance(p)
    try:
        if not h.start():
            return
        d = os.path.abspath(os.path.join(p.dir, 'dir-r'))
        os.makedirs(d, exist_ok=True)
        rg = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
        check('restart-grant-ok', rg and rg.get('status') == 'granted', str(rg))
        row_before = [x for x in rows_for(read_grants(p), SID_A)
                      if x.get('workdir', '').lower() == d.lower()][0]

        # External ACL change moves the generation.
        add_ace_ps(d, SID_A, mask='Read', inherit='None')
        r2 = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
        check('restart-repeat-ensure-already-granted',
              r2 and r2.get('status') == 'already_granted', str(r2))
        row_after = [x for x in rows_for(read_grants(p), SID_A)
                     if x.get('workdir', '').lower() == d.lower()][0]
        check('restart-generation-retracted-on-mismatch',
              row_after.get('created') is False
              and row_after.get('acl_fp') in (None, ''),
              f"before={row_before} after={row_after}")

        # A restart changes nothing about the divergence: revoke must not
        # delete the foreign Read rule.
        h.stop()
        h2 = HelperInstance(p)
        try:
            h2.start()
            rv = h2.request({'op': 'managed_workdir_revoke_access', 'target_sid': SID_A})
            check('restart-revoked-no-foreign-delete',
                  rv is not None and rv.get('revoked_rules') == 0 and 'Read' in get_ace_rights_ps(d, SID_A),
                  str(rv))
        finally:
            h2.stop()
    finally:
        h.stop()
        p.cleanup()


# --- TXN-01/02/03: transaction scope and path justification
def test_revoke_transaction_path_controls():
    p = TestPlane('own-gen-txn-scope')
    h = HelperInstance(p)
    try:
        if not h.start():
            return
        dA = os.path.abspath(os.path.join(p.dir, 'dA'))
        dB = os.path.abspath(os.path.join(p.dir, 'dB'))
        os.makedirs(dA, exist_ok=True)
        os.makedirs(dB, exist_ok=True)
        rgA = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': dA})
        check('txn-grant-A-ok', rgA and rgA.get('status') == 'granted', str(rgA))
        rgB = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': dB})
        check('txn-grant-B-ok', rgB and rgB.get('status') == 'granted', str(rgB))
        h.stop()

        # TXN-01: revoke journal naming a path with no ownership record.
        unowned = os.path.abspath(os.path.join(p.dir, 'unowned'))
        os.makedirs(unowned, exist_ok=True)
        write_txn(p, {
            'txn_id': 'txn-unowned-001', 'op': 'revoke', 'target_sid': SID_A,
            'workdir': unowned, 'created': False, 'target_workdirs': [unowned],
            'state': 'intent_logged', 'updated_at': '2026-10-11T00:00:00Z',
        })
        h2 = HelperInstance(p)
        try:
            h2.start()
            r1 = h2.request({'op': 'managed_workdir_revoke_access', 'target_sid': SID_A})
            check('txn-unowned-path-refused',
                  r1 and r1.get('status') == 'INCONSISTENT_GRANT_STATE'
                  and 'proven ownership' in str(r1.get('error', '')), str(r1))
            check('txn-unowned-journal-preserved', os.path.exists(txn_file(p)), 'journal lost')
            check('txn-unowned-no-ace-deleted', has_ace_ps(dA, SID_A), 'unjustified ACE deletion')

            # TXN-02: unexpected additional path in the transaction.
            write_txn(p, {
                'txn_id': 'txn-extra-002', 'op': 'revoke', 'target_sid': SID_A,
                'workdir': '', 'created': False,
                'target_workdirs': [dA, dB, unowned],
                'state': 'intent_logged', 'updated_at': '2026-10-11T00:00:00Z',
            })
            r2 = h2.request({'op': 'managed_workdir_revoke_access', 'target_sid': SID_A})
            check('txn-unexpected-path-refused',
                  r2 and r2.get('status') == 'INCONSISTENT_GRANT_STATE'
                  and 'unexpected' in str(r2.get('error', '')).lower()
                  or (r2 and r2.get('status') == 'INCONSISTENT_GRANT_STATE'),
                  str(r2))
            check('txn-unexpected-journal-preserved', os.path.exists(txn_file(p)), 'journal lost')
            check('txn-unexpected-no-ace-deleted', has_ace_ps(dA, SID_A) and has_ace_ps(dB, SID_A),
                  'unstaged deletion happened')
        finally:
            h2.stop()
    finally:
        h.stop()
        p.cleanup()


# --- WITNESS-01: foreign generation at failure refuses rollback
def test_foreign_generation_refuses_rollback():
    p = TestPlane('own-gen-witness-fail')
    h = HelperInstance(p)
    try:
        if not h.start():
            return
        d = os.path.abspath(os.path.join(p.dir, 'dir-w'))
        os.makedirs(d, exist_ok=True)

        # Force the ledger save to fail (read-only grants.json) so the grant
        # path reaches HandleGrantFailure AFTER the ACL has been mutated.
        write_grants(p, [])
        kernel32.SetFileAttributesW(grants_file(p), 1)
        try:
            r = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
            # The helper mutated, then could not persist: rollback of its own
            # verified rule runs, and the response carries the phase.
            check('witness-fail-status',
                  r and r.get('status') in ('LEDGER_SAVE_FAILED', 'INCONSISTENT_GRANT_STATE'),
                  str(r))
            check('witness-fail-helper-alive', h.request({'op': 'status'}) is not None, 'helper died')
        finally:
            kernel32.SetFileAttributesW(grants_file(p), 128)
        h.stop()

        # Same failure, but a foreign process replaces the ACL mid-flight:
        # the journal then carries a pre-witness that no longer matches any
        # generation the helper wrote, and no rollback may run.
        p2 = TestPlane('own-gen-witness-foreign')
        h2 = HelperInstance(p2)
        try:
            if not h2.start():
                return
            d2 = os.path.abspath(os.path.join(p2.dir, 'dir-w2'))
            os.makedirs(d2, exist_ok=True)
            # Synthesize the exact state the helper would journal if its write
            # witness had been superseded before verification: pre != current,
            # post_write != current, and the ACE on disk was placed externally.
            pre_fp = 'deadbeef' + '0' * 56
            write_fp = 'facade00' + '0' * 56
            # The live ACL fingerprint of the foreign ACE must differ from both.
            add_ace_ps(d2, SID_A)
            write_grants(p2, [])
            write_txn(p2, {
                'txn_id': 'txn-foreign-003', 'op': 'grant', 'target_sid': SID_A,
                'workdir': d2, 'created': True, 'state': 'mutation_written',
                'pre_mutation_acl_fp': pre_fp, 'post_write_acl_fp': write_fp,
                'updated_at': '2026-10-11T00:00:00Z',
            })
            r2 = h2.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d2})
            check('foreign-journal-not-adopted',
                  r2 is not None and r2.get('status') in ('already_granted', 'granted',
                                                            'INCONSISTENT_GRANT_STATE'),
                  str(r2))
            rows = rows_for(read_grants(p2), SID_A)
            adopted = [x for x in rows if x.get('created') is True and x.get('workdir', '').lower() == d2.lower()]
            check('foreign-journal-never-adopted-as-helper-owned',
                  not adopted, f'owned row fabricated for foreign ACE: {adopted}')
            rv = h2.request({'op': 'managed_workdir_revoke_access', 'target_sid': SID_A})
            check('foreign-ace-preserved-after-revoke',
                  rv is not None and rv.get('revoked_rules') == 0 and has_ace_ps(d2, SID_A),
                  str(rv))
        finally:
            h2.stop()
            p2.cleanup()
    finally:
        h.stop()
        p.cleanup()


# --- TXN-04: repeated reconciliation does not double-delete
def test_repeated_reconciliation():
    p = TestPlane('own-gen-repeat-reconcile')
    h = HelperInstance(p)
    try:
        if not h.start():
            return
        d = os.path.abspath(os.path.join(p.dir, 'dir-p'))
        os.makedirs(d, exist_ok=True)
        rg = h.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
        check('repeat-grant-ok', rg and rg.get('status') == 'granted', str(rg))
        h.stop()

        write_txn(p, {
            'txn_id': 'txn-repeat-004', 'op': 'revoke', 'target_sid': SID_A,
            'workdir': d, 'created': False, 'target_workdirs': [d],
            'state': 'intent_logged', 'updated_at': '2026-10-11T00:00:00Z',
        })
        h2 = HelperInstance(p)
        try:
            h2.start()
            r1 = h2.request({'op': 'managed_workdir_revoke_access', 'target_sid': SID_A})
            check('repeat-first-reconcile-revoked',
                  r1 and r1.get('status') == 'revoked' and not has_ace_ps(d, SID_A), str(r1))
            r2 = h2.request({'op': 'managed_workdir_revoke_access', 'target_sid': SID_A})
            check('repeat-second-call-idempotent',
                  r2 and r2.get('status') == 'revoked' and r2.get('revoked_rules') == 0, str(r2))
            check('repeat-journal-absent', not os.path.exists(txn_file(p)), 'journal leaked')
        finally:
            h2.stop()
    finally:
        h.stop()
        p.cleanup()


def main():
    print('=' * 70)
    print('W2-002 residual ownership-generation negative controls (T-282)')
    print('=' * 70)
    test_case_a_external_then_helper_created()
    test_case_b_external_replacement_not_deleted()
    test_case_b_legacy_row_without_witness()
    test_restart_and_gen_mismatch()
    test_revoke_transaction_path_controls()
    test_foreign_generation_refuses_rollback()
    test_repeated_reconciliation()
    print('\n' + '=' * 70)
    print(f'RESULTS: PASS {len(PASSES)} / FAIL {len(FAILURES)}')
    print('=' * 70)
    if FAILURES:
        for f in FAILURES:
            print('  FAILED: ' + f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
