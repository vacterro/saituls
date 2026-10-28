#!/usr/bin/env python3
"""Kill actual disposable Windows helpers at verified deterministic checkpoints.

PROCESS_TERMINATION_PASS proves ordinary process-crash recovery, never physical
power-loss durability. ACLs use fabricated identities; no Windows account is
created, removed, or accessed. Only Popen-owned PIDs may be terminated.
"""
import json
import os
from pathlib import Path
import sys
import threading
import uuid

from test_accounts_journal_authority import wait_checkpoint
from test_accounts_security_fault_injection import TestPlane, HelperInstance, add_ace_ps, has_ace_ps

SID = 'S-1-5-21-444444444-555555555-666666666-2101'
FOREIGN = 'S-1-5-21-444444444-555555555-666666666-2102'
RESULTS = []


def check(name, condition, detail=''):
    RESULTS.append(dict(name=name, passed=bool(condition), detail=str(detail),
                        classification='PROCESS_TERMINATION_PASS' if condition else 'FAIL'))
    print(('PASS  ' if condition else 'FAIL  ') + name, flush=True)
    if not condition:
        print('      ' + str(detail), flush=True)


def scenario(point, state, owned, mutate_foreign=False):
    name = point + ('-foreign' if mutate_foreign else '')
    p = TestPlane('journal-kill-' + uuid.uuid4().hex)
    p.env['SAI_ACCOUNTS_JOURNAL_CHECKPOINT'] = point
    helper = HelperInstance(p)
    work = Path(p.dir) / 'work'
    work.mkdir()
    txn = Path(p.dir) / 'grants.txn.json'
    request = dict(op='managed_workdir_ensure_access', target_sid=SID, workdir=str(work))
    responses = []
    def run_request():
        try:
            responses.append(helper.request(request))
        except OSError:
            responses.append(None)
    try:
        check(name + '-started', helper.start())
        worker = threading.Thread(target=run_request, daemon=True)
        worker.start()
        marker, checkpoint = wait_checkpoint(p.dir, helper.proc, point.split('@')[0])
        before = txn.read_bytes()
        record = json.loads(before)
        check(name + '-authoritative-state', record['state'] == state and bool(record.get('txn_id'))
              and record['op'] == 'grant' and record['target_sid'] == SID and record['workdir'] == str(work), record)
        check(name + '-checkpoint-pid', checkpoint['pid'] == helper.proc.pid, checkpoint)
        if owned:
            check(name + '-published-write-witness', bool(record.get('post_write_acl_fp')), record)
        helper.proc.kill()
        helper.proc.wait(timeout=5)
        worker.join(timeout=5)
        check(name + '-killed-no-success', helper.proc.returncode != 0 and not worker.is_alive()
              and all(r is None or r.get('status') not in ('granted', 'already_granted') for r in responses), responses)
        check(name + '-journal-intact-after-kill', txn.read_bytes() == before)
        if point == 'stage-write@2':
            staged = list(Path(p.dir).glob('grants.txn.json.tmp.*'))
            check(name + '-partial-stage-is-not-authority', len(staged) == 1 and staged[0].read_bytes() == b'{')
        if point == 'post-flush@2':
            staged = list(Path(p.dir).glob('grants.txn.json.tmp.*'))
            check(name + '-flushed-stage-is-not-authority', len(staged) == 1
                  and json.loads(staged[0].read_bytes())['state'] == 'mutation_written'
                  and json.loads(staged[0].read_bytes())['txn_id'] == record['txn_id'])
        if mutate_foreign:
            check(name + '-foreign-generation-installed', bool(add_ace_ps(str(work), FOREIGN, mask='ReadAndExecute')))
        p.env.pop('SAI_ACCOUNTS_JOURNAL_CHECKPOINT')
        restart = HelperInstance(p, pipe_name='sai-journal-restart-' + uuid.uuid4().hex)
        try:
            check(name + '-restart-started', restart.start())
            r = restart.request(request)
            if mutate_foreign:
                check(name + '-foreign-recovery-refuses', r is not None
                      and r.get('status') == 'INCONSISTENT_GRANT_STATE' and txn.read_bytes() == before, r)
                r2 = restart.request(request)
                check(name + '-foreign-refusal-repeatable', r2 is not None
                      and r2.get('status') == 'INCONSISTENT_GRANT_STATE' and txn.read_bytes() == before, r2)
                revoked = restart.request(dict(op='managed_workdir_revoke_access', target_sid=SID))
                check(name + '-foreign-revoke-refuses', revoked is not None
                      and revoked.get('status') == 'INCONSISTENT_GRANT_STATE' and txn.read_bytes() == before, revoked)
                check(name + '-both-foreign-rules-survive', has_ace_ps(str(work), SID) and has_ace_ps(str(work), FOREIGN))
                return
            check(name + '-justified-recovery', r is not None and r.get('status') in ('granted', 'already_granted'), r)
            rows = json.loads((Path(p.dir) / 'grants.json').read_text(encoding='utf-8-sig'))
            row = next(r for r in rows if r['target_sid'] == SID and r['workdir'] == str(work))
            expected_owned = owned and not mutate_foreign
            # A kill before ACL mutation restarts a fresh authorized transaction.
            if point == 'after-publish@1':
                expected_owned = True
            check(name + '-ownership-supported-by-witness', row['created'] is expected_owned, row)
            gen = row.get('gen')
            r2 = restart.request(request)
            rows2 = json.loads((Path(p.dir) / 'grants.json').read_text(encoding='utf-8-sig'))
            row2 = next(r for r in rows2 if r['target_sid'] == SID and r['workdir'] == str(work))
            check(name + '-recovery-repeatable', r2 is not None and r2.get('status') == 'already_granted'
                  and row2.get('gen') == gen and row2['created'] == row['created'] and not txn.exists(), r2)
            revoked = restart.request(dict(op='managed_workdir_revoke_access', target_sid=SID))
            remains = has_ace_ps(str(work), SID)
            check(name + '-revoke-respects-generation', revoked is not None and revoked.get('status') == 'revoked'
                  and remains is (not expected_owned), revoked)
            if mutate_foreign:
                check(name + '-foreign-ace-survives', has_ace_ps(str(work), FOREIGN))
        finally:
            restart.stop()
    finally:
        helper.stop()
        p.cleanup()


def main():
    for point, state, owned in (
        ('after-publish@1', 'intent_logged', False),
        ('after-acl', 'intent_logged', False),
        ('pre-stage@2', 'intent_logged', False),
        ('stage-write@2', 'intent_logged', False),
        ('post-flush@2', 'intent_logged', False),
        ('after-publish@2', 'mutation_written', True),
        ('after-publish@3', 'ace_verified', True),
        ('ledger-committed', 'ace_verified', True),
    ):
        scenario(point, state, owned)
    scenario('after-publish@2', 'mutation_written', True, mutate_foreign=True)
    passed = sum(r['passed'] for r in RESULTS)
    print('RESULTS: EXECUTED %d / PASS %d / FAIL %d / SKIP 0' % (len(RESULTS), passed, len(RESULTS)-passed))
    report = os.environ.get('SAI_JOURNAL_TEST_REPORT')
    if report:
        Path(report).write_text(json.dumps(RESULTS, indent=2), encoding='utf-8')
    return int(passed != len(RESULTS))


if __name__ == '__main__':
    sys.exit(main())
