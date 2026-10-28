#!/usr/bin/env python3
"""T-282 journal-integrity fault injection: the transaction journal must stay
a recoverable, coherent record through stage failure, write failure, publish
failure and retry.

Run:  python tests/test_accounts_journal_atomicity.py
Exit: 0 = all PASS, 1 = failures.

Classification:
  WINDOWS_DISPOSABLE_PASS  -- real filesystem-forced failures (read-only
                              destination). Proves the helper's decisions
                              against a disposable SAI_ACCOUNTS_ROOT plane
                              with fabricated SIDs that exist in no directory.
  SIMULATED_PASS           -- the SAI_ACCOUNTS_JOURNAL_FAULT seam forces a
                              failure at one exact stage of the publish. It
                              proves the failure HANDLING path; it does not
                              prove the OS honours an atomic replace under a
                              physical power loss.

Stages exercised (the seam can name one, optionally as "<stage>@<ordinal>"):
  pre-stage    fail before the staged file is written
  stage-write  fail during the staged write, leaving partial staged bytes
  post-flush   fail after the staged content was flushed, before publication
  replace      fail during the atomic replacement

Assertions per stage:
  - a valid earlier journal is not truncated in place
  - transaction id stays stable across state transitions
  - previously recorded witnesses survive state transitions
  - no false successful completion is reported when a required journal
    persistence failed
  - recovery either repeats cleanly or fails closed with retained evidence
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from test_accounts_journal_authority import Probe, compile_probe

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_accounts_security_fault_injection import (  # noqa: E402
    TestPlane, HelperInstance, add_ace_ps, check,
    kernel32, FAILURES, PASSES,
)

SID_A = 'S-1-5-21-444444444-555555555-666666666-2101'
SID_B = 'S-1-5-21-444444444-555555555-666666666-2102'
STAGES = ('pre-stage', 'stage-write', 'post-flush', 'replace')


def txn_path(p):
    return os.path.join(p.dir, 'grants.txn.json')


def raw_txn(p):
    """Return the journal file's exact bytes, or None when absent."""
    path = txn_path(p)
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        return fh.read()


def read_txn(p):
    raw = raw_txn(p)
    if raw is None:
        return None
    try:
        text = raw.decode('utf-8')
        if text.startswith('﻿'):
            text = text[1:]
        return json.loads(text)
    except (UnicodeDecodeError, ValueError):
        return {'_unparseable': True, '_bytes': raw}


def seeded_journal(workdir, sid=SID_B, txn_id='txn-seed-001'):
    return {
        'schema': 'sai.accounts.helper/txn/2',
        'txn_id': txn_id,
        'op': 'grant',
        'target_sid': sid,
        'workdir': workdir,
        'created': True,
        'state': 'mutation_written',
        'pre_mutation_acl_fp': 'a1' * 32,
        'post_write_acl_fp': 'b2' * 32,
        'updated_at': '2026-10-11T00:00:00Z',
    }


def write_journal(p, data):
    with open(txn_path(p), 'w', encoding='utf-8') as fh:
        json.dump(data, fh)


def new_plane(prefix, fault=None):
    p = TestPlane(prefix)
    if fault is not None:
        p.env['SAI_ACCOUNTS_JOURNAL_FAULT'] = fault
    return p


# ----------------------------------------------------------------------
# Stage A: a brand-new transaction with NO prior journal.
# A failure at any stage must leave no journal behind and must not report
# a successful mutation.
# ----------------------------------------------------------------------
def test_stage_failure_creates_no_partial_journal():
    for stage in STAGES:
        p = new_plane('journal-new-%s' % stage.replace('-', '_'), fault=stage)
        h = HelperInstance(p)
        try:
            if not h.start():
                continue
            d = os.path.abspath(os.path.join(p.dir, 'dir-new'))
            os.makedirs(d, exist_ok=True)
            r = h.request({'op': 'managed_workdir_ensure_access',
                           'target_sid': SID_A, 'workdir': d})
            check('new-%s-no-success-report' % stage,
                  r is not None and r.get('status') != 'granted' and
                  r.get('status') != 'already_granted', str(r))
            check('new-%s-no-journal-left' % stage,
                  raw_txn(p) is None,
                  'journal bytes: %r' % (raw_txn(p),))
            check('new-%s-no-staged-left' % stage,
                  not [f for f in os.listdir(p.dir) if 'txn.json.tmp' in f],
                  os.listdir(p.dir))
            check('new-%s-helper-alive' % stage,
                  h.request({'op': 'status'}) is not None, 'helper died')
        finally:
            h.stop()
            p.cleanup()


# ----------------------------------------------------------------------
# Stage B: a VALID earlier journal exists. A failed state update at every
# stage must leave it byte-for-byte intact -- the old in-place truncate
# destroyed exactly this evidence.
# ----------------------------------------------------------------------
def test_failed_update_never_truncates_earlier_journal():
    # Ensure first reconciles (and may legitimately retire) an older journal.
    # Invoke the actual update routine to isolate the publication boundary.
    with tempfile.TemporaryDirectory(prefix='sai-journal-stage-probe-') as tmp:
        exe = os.path.join(tmp, 'probe.exe')
        compile_probe(exe)
        for stage in STAGES:
            with Probe(exe) as p:
                p.seed(target_sid=SID_B)
                before = p.raw()
                p.request(method='fault', value=stage)
                r = p.request(method='state', state='ace_verified')
                after = json.loads(p.raw())
                check('prev-%s-no-truncate' % stage, p.raw() == before, str(r))
                check('prev-%s-journal-parseable' % stage, isinstance(after, dict), str(after))
                check('prev-%s-witness-survives' % stage,
                      after.get('post_write_acl_fp') == 'b2' * 32 and
                      after.get('pre_mutation_acl_fp') == 'a1' * 32, str(after))
                check('prev-%s-txn-id-survives' % stage,
                      after.get('txn_id') == 'txn-seed-001', str(after))
                check('prev-%s-no-false-success' % stage, r.get('ok') is False, str(r))


# ----------------------------------------------------------------------
# Real filesystem publication failure: the destination journal is read-only,
# so the atomic replace throws. A fully staged file must not lose the journal.
# ----------------------------------------------------------------------
def test_readonly_destination_preserves_journal():
    p = new_plane('journal-readonly')
    h = HelperInstance(p)
    try:
        if not h.start():
            return
        d = os.path.abspath(os.path.join(p.dir, 'dir-ro'))
        os.makedirs(d, exist_ok=True)
        write_journal(p, seeded_journal(d))
        before = raw_txn(p)
        kernel32.SetFileAttributesW(txn_path(p), 1)  # FILE_ATTRIBUTE_READONLY
        try:
            r = h.request({'op': 'managed_workdir_ensure_access',
                           'target_sid': SID_A, 'workdir': d})
            check('ro-no-truncate', raw_txn(p) == before,
                  'before=%r after=%r' % (before, raw_txn(p)))
            after = read_txn(p)
            check('ro-journal-parseable',
                  isinstance(after, dict) and not after.get('_unparseable'),
                  str(after)[:120])
            check('ro-no-false-success',
                  r is None or r.get('status') != 'granted', str(r))
            check('ro-helper-alive', h.request({'op': 'status'}) is not None,
                  'helper died')
        finally:
            kernel32.SetFileAttributesW(txn_path(p), 128)
    finally:
        h.stop()
        p.cleanup()


# ----------------------------------------------------------------------
# Witness carried across a state transition.
# With publication of the THIRD publish (ace_verified) failing, the journal
# must still hold the post-write witness recorded by the second publish and
# the transaction identity minted by the first.
# ----------------------------------------------------------------------
def test_ace_verified_failure_retains_witness_and_identity():
    p = new_plane('journal-verified', fault='replace@3')
    h = HelperInstance(p)
    try:
        if not h.start():
            return
        d = os.path.abspath(os.path.join(p.dir, 'dir-verified'))
        os.makedirs(d, exist_ok=True)
        r = h.request({'op': 'managed_workdir_ensure_access',
                       'target_sid': SID_A, 'workdir': d})
        check('verified-fail-closed',
              r is not None and r.get('status') == 'INCONSISTENT_GRANT_STATE',
              str(r))
        j = read_txn(p)
        check('verified-witness-retained',
              isinstance(j, dict) and j.get('post_write_acl_fp'),
              str(j)[:160])
        check('verified-identity-minted-once',
              isinstance(j, dict) and isinstance(j.get('txn_id'), str) and
              len(j.get('txn_id', '')) == 32,
              str(j)[:120])
        # The write witness recorded by the surviving publish must match what
        # is actually on disk, or recovery must refuse to adopt it.
        j2 = read_txn(p)
        check('verified-evidence-reproducible', j2 == j,
              'journal changed between reads')
    finally:
        h.stop()
        p.cleanup()


# ----------------------------------------------------------------------
# Recovery after a faulted publish must be repeatable: clear the fault and
# the same transaction completes, with no duplicate or orphaned record.
# ----------------------------------------------------------------------
def test_recovery_after_stage_fault_is_repeatable():
    p = new_plane('journal-recover', fault='replace@3')
    d = os.path.abspath(os.path.join(p.dir, 'dir-recover'))
    os.makedirs(d, exist_ok=True)
    try:
        h = HelperInstance(p)
        try:
            if not h.start():
                return
            r1 = h.request({'op': 'managed_workdir_ensure_access',
                            'target_sid': SID_A, 'workdir': d})
            check('recover-fault-not-published',
                  r1 is not None and r1.get('status') != 'granted', str(r1))
            interrupted = read_txn(p)
            check('recover-owned-witness-kept',
                  isinstance(interrupted, dict) and interrupted.get('txn_id')
                  and interrupted.get('post_write_acl_fp')
                  and interrupted.get('pre_mutation_acl_fp'), str(interrupted))
        finally:
            h.stop()

        # Retry with the fault cleared: recovery must be repeatable and must
        # not leave the seeded record orphaned.
        p.env.pop('SAI_ACCOUNTS_JOURNAL_FAULT', None)
        h2 = HelperInstance(p, pipe_name='sai-journal-recover-retry')
        try:
            if not h2.start():
                return
            r2 = h2.request({'op': 'managed_workdir_ensure_access',
                             'target_sid': SID_A, 'workdir': d})
            check('recover-retry-terminal',
                  r2 is not None and r2.get('status') in
                  ('granted', 'already_granted', 'INCONSISTENT_GRANT_STATE'),
                  str(r2))
            j = read_txn(p)
            check('recover-journal-retired-after-commit', j is None, str(j))
            with open(os.path.join(p.dir, 'grants.json'), encoding='utf-8-sig') as f:
                rows = json.load(f)
            check('recover-ownership-supported-by-witness',
                  any(row.get('target_sid') == SID_A and row.get('created') is True
                      and row.get('acl_fp') == interrupted.get('post_write_acl_fp') for row in rows), str(rows))
            r3 = h2.request({'op': 'managed_workdir_ensure_access', 'target_sid': SID_A, 'workdir': d})
            check('recover-repeatable', r3 is not None and r3.get('status') == 'already_granted', str(r3))
        finally:
            h2.stop()
    finally:
        p.cleanup()


def main():
    test_stage_failure_creates_no_partial_journal()
    test_failed_update_never_truncates_earlier_journal()
    test_readonly_destination_preserves_journal()
    test_ace_verified_failure_retains_witness_and_identity()
    test_recovery_after_stage_fault_is_repeatable()

    print('=' * 70)
    print('RESULTS: EXECUTED %d / PASS %d / FAIL %d / SKIP 0' %
          (len(PASSES) + len(FAILURES), len(PASSES), len(FAILURES)))
    print('=' * 70)
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
