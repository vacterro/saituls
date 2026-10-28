#!/usr/bin/env python3
"""Execute journal authority and publication controls against the built helper.

The reflection probe invokes the actual assembly, not a reimplementation.
All artifacts and fabricated-SID ACLs live under a fresh Windows temp root.
Fault-seam cases are SIMULATED_PASS; filesystem refusal is a disposable
Windows control. This suite never claims physical power-loss durability.
"""
import json
import ctypes
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
BIN = Path(os.environ.get('SAI_ACCOUNTS_TEST_BIN', ROOT / 'Scripts/accounts/bin'))
SID = 'S-1-5-21-444444444-555555555-666666666-2101'
OTHER_SID = 'S-1-5-21-444444444-555555555-666666666-2102'
STAGES = ('pre-stage', 'stage-write', 'post-flush', 'replace')
RESULTS = []


def check(name, condition, detail='', classification='WINDOWS_DISPOSABLE_PASS'):
    RESULTS.append(dict(name=name, passed=bool(condition), detail=detail,
                        classification=classification if condition else 'FAIL'))
    print(('PASS  ' if condition else 'FAIL  ') + name, flush=True)
    if not condition:
        print('      ' + str(detail), flush=True)


def compile_probe(out):
    csc = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    r = subprocess.run([str(csc), '-nologo', '-r:System.Web.Extensions.dll',
                        '-out:' + str(out), str(ROOT / 'tests/accounts_journal_probe.cs')],
                       capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(r.stdout + r.stderr)


class Probe:
    def __init__(self, executable, env=None):
        self.root = Path(tempfile.mkdtemp(prefix='sai-journal-authority-'))
        self.workdir = self.root / 'work'
        self.workdir.mkdir()
        self.txn = self.root / 'grants.txn.json'
        self.env = dict(os.environ, SAI_ACCOUNTS_ROOT=str(self.root), SAI_HELPER_TEST_MODE='1')
        self.env.pop('SAI_ACCOUNTS_JOURNAL_FAULT', None)
        self.env.pop('SAI_ACCOUNTS_JOURNAL_CHECKPOINT', None)
        if env:
            self.env.update(env)
        self.stderr = tempfile.TemporaryFile(mode='w+t', encoding='utf-8')
        self.proc = subprocess.Popen([str(executable), str(BIN / 'sai-accounts-helper.exe')],
                                     env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.stderr, text=True, creationflags=0x08000000)
        self.responses = queue.Queue()
        def read():
            for line in self.proc.stdout:
                self.responses.put(json.loads(line))
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def send(self, **command):
        self.proc.stdin.write(json.dumps(command) + '\n')
        self.proc.stdin.flush()

    def result(self):
        try:
            return self.responses.get(timeout=20)
        except queue.Empty:
            self.proc.kill()
            raise RuntimeError('journal probe produced no result within 20 seconds')

    def request(self, **command):
        self.send(**command)
        return self.result()

    def raw(self):
        return self.txn.read_bytes() if self.txn.exists() else None

    def seed(self, **changes):
        d = dict(schema='sai.accounts.helper/txn/2', txn_id='txn-seed-001', op='grant',
                 target_sid=SID, workdir=str(self.workdir), created=True, state='mutation_written',
                 pre_mutation_acl_fp='a1' * 32, post_write_acl_fp='b2' * 32,
                 updated_at='2026-10-11T00:00:00Z')
        d.update(changes)
        self.txn.write_text(json.dumps(d), encoding='utf-8')
        return d

    def begin(self):
        return self.request(method='update', begin=True, state='intent_logged', op='grant',
                            sid=SID, workdir=str(self.workdir), created=True, pre='a1' * 32)

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        self.stderr.close()
        shutil.rmtree(self.root)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def identity_controls(exe):
    for label, raw in [('missing', None), ('null', b'null'), ('array', b'[]'),
                       ('string', b'"historical"'), ('truncated', b'{"txn_id":'),
                       ('empty-object', b'{}')]:
        with Probe(exe) as p:
            if raw is not None:
                p.txn.write_bytes(raw)
            r = p.request(method='state', state='ace_verified', error='probe')
            check('authority-' + label + '-refused', r.get('ok') is False and p.raw() == raw, r)
    for label, changes in [('future-schema', dict(schema='sai.accounts.helper/txn/99')),
                           ('missing-id', dict(txn_id=None)), ('missing-schema', dict(schema=None)),
                           ('bad-sid', dict(target_sid='not-a-sid')), ('unknown-op', dict(op='other'))]:
        with Probe(exe) as p:
            p.seed(**changes)
            before = p.raw()
            r = p.request(method='state', state='ace_verified')
            check('authority-' + label + '-refused', r.get('ok') is False and p.raw() == before, r)
    with Probe(exe) as p:
        check('explicit-begin-allowed', p.begin().get('ok') is True)
        before = json.loads(p.raw())
        r = p.request(method='update', state='mutation_written', op='grant', sid=SID,
                      workdir=str(p.workdir), created=True, write='b2' * 32)
        r2 = p.request(method='update', state='ace_verified', op='grant', sid=SID,
                       workdir=str(p.workdir), created=True, post='b2' * 32)
        after = json.loads(p.raw())
        check('valid-transitions-stable-identity', r.get('ok') and r2.get('ok') and
              all(before[k] == after[k] for k in ('txn_id', 'op', 'target_sid', 'workdir', 'created')), after)
        check('valid-transitions-preserve-witnesses', after.get('post_write_acl_fp') == 'b2' * 32
              and after.get('pre_mutation_acl_fp') == 'a1' * 32, after)
    for label, changes in [('op', dict(op='revoke')), ('sid', dict(sid=OTHER_SID)),
                           ('workdir', dict(workdir='C:\\foreign')), ('created', dict(created=False)),
                           ('target-scope', dict(targets=['C:\\foreign'])), ('pre-witness', dict(pre='ff' * 32))]:
        with Probe(exe) as p:
            p.begin()
            before = p.raw()
            r = p.request(method='update', state='ace_verified', **changes)
            check('scope-' + label + '-immutable', r.get('ok') is False and p.raw() == before, r)
    with Probe(exe) as p:
        p.begin()
        p.seed(txn_id='foreign-replacement')
        before = p.raw()
        r = p.request(method='state', state='ace_verified')
        check('foreign-transaction-refused', r.get('ok') is False and p.raw() == before, r)
    with Probe(exe) as p:
        p.begin()
        before = p.raw()
        r = p.request(method='state', state=None)
        check('empty-transition-state-refused', r.get('ok') is False and p.raw() == before, r)
    with Probe(exe) as p:
        p.begin()
        p.request(method='update', state='ace_verified', post='b2' * 32)
        before = p.raw()
        r = p.request(method='update', state='ace_verified', post='ff' * 32)
        check('accumulated-verified-witness-immutable', r.get('ok') is False and p.raw() == before, r)


def publication_controls(exe):
    for stage in STAGES:
        for qualified in (False, True):
            with Probe(exe) as p:
                p.begin()
                before = p.raw()
                fault = stage + ('@2' if qualified else '')
                p.request(method='fault', value=fault)
                r = p.request(method='state', state='ace_verified', error='probe')
                check('publish-' + fault + '-old-authority-intact', r.get('ok') is False
                      and p.raw() == before and not list(p.root.glob('grants.txn.json.tmp.*')), r,
                      classification='SIMULATED_PASS')
                if qualified:
                    r = p.request(method='state', state='ace_verified')
                    check('publish-' + fault + '-only-selected-ordinal', r.get('ok') is True,
                          r, classification='SIMULATED_PASS')
        with Probe(exe) as p:
            p.request(method='fault', value=stage)
            r = p.begin()
            check('publish-' + stage + '-new-failure-no-partial', r.get('ok') is False
                  and p.raw() is None and not list(p.root.glob('grants.txn.json.tmp.*')), r,
                  classification='SIMULATED_PASS')
    with Probe(exe) as p:
        p.begin()
        before = json.loads(p.raw())
        p.request(method='state', state='mutation_written')
        after = json.loads(p.raw())
        check('successful-replacement-stable-authority', after['state'] == 'mutation_written'
              and all(before[k] == after[k] for k in ('txn_id', 'op', 'target_sid', 'workdir', 'created')), after)
    for method in ('state', 'failed'):
        with Probe(exe) as p:
            p.begin()
            before = p.raw()
            p.request(method='fault', value='post-flush')
            r = p.request(method=method, state='inconsistent', error='fault',
                          op='grant', sid=SID, workdir=str(p.workdir))
            check('failure-stamp-' + method + '-observable', r.get('ok') is False
                  and p.raw() == before, r, classification='SIMULATED_PASS')


def rollback_scope_control(exe):
    with Probe(exe) as p:
        ps = ("$sid = New-Object System.Security.Principal.SecurityIdentifier('%s'); "
              "$acl = Get-Acl -LiteralPath '%s'; "
              "$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule"
              "($sid, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))); "
              "Set-Acl -LiteralPath '%s' -AclObject $acl") % (SID, p.workdir, p.workdir)
        env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
        subprocess.run(['powershell', '-NoProfile', '-Command', ps], env=env, check=True,
                       capture_output=True, text=True, timeout=20)
        fp = p.request(method='fingerprint', workdir=str(p.workdir))['value']
        p.seed(target_sid=OTHER_SID, post_write_acl_fp=fp)
        before = p.raw()
        r = p.request(method='failure', workdir=str(p.workdir), sid=SID)
        after_fp = p.request(method='fingerprint', workdir=str(p.workdir))['value']
        check('foreign-scope-cannot-authorize-rollback', after_fp == fp and p.raw() == before
              and r.get('status') == 'INCONSISTENT_GRANT_STATE', r)


def recovery_scope_control(exe):
    with Probe(exe) as p:
        ps = ("$ErrorActionPreference='Stop'; "
              "$sid = New-Object System.Security.Principal.SecurityIdentifier('%s'); "
              "$acl = Get-Acl -LiteralPath '%s'; "
              "$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule"
              "($sid, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))); "
              "Set-Acl -LiteralPath '%s' -AclObject $acl") % (SID, p.workdir, p.workdir)
        env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
        subprocess.run(['powershell', '-NoProfile', '-Command', ps], env=env, check=True,
                       capture_output=True, text=True, timeout=20)
        fp = p.request(method='fingerprint', workdir=str(p.workdir))['value']
        p.seed(post_write_acl_fp='b2' * 32)
        before = p.raw()
        r = p.request(method='reconcile')
        after_fp = p.request(method='fingerprint', workdir=str(p.workdir))['value']
        check('foreign-recovery-preserves-journal-authority', r.get('ok') is False
              and p.raw() == before and after_fp == fp, r)


def wait_checkpoint(root, proc, stage):
    marker = Path(root) / 'grants.txn.json.checkpoint'
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError('process exited before checkpoint: ' + stage)
        try:
            d = json.loads(marker.read_text(encoding='utf-8-sig'))
            if d['stage'] == stage and d['pid'] == proc.pid:
                return marker, d
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(.01)
    raise RuntimeError('checkpoint not reached: ' + stage)


def replacement_controls(exe):
    for kind in ('foreign-id', 'same-id-new-generation', 'missing'):
        with Probe(exe) as p:
            p.begin()
            p.request(method='checkpoint', value='after-load')
            p.send(method='state', state='ace_verified')
            marker, _ = wait_checkpoint(p.root, p.proc, 'after-load')
            replacement = json.loads(p.raw())
            if kind == 'missing':
                p.txn.unlink()
            else:
                if kind == 'foreign-id':
                    replacement['txn_id'] = 'foreign-concurrent-transaction'
                else:
                    replacement['post_write_acl_fp'] = 'ff' * 32
                stage = p.root / 'foreign.staged'
                stage.write_text(json.dumps(replacement), encoding='utf-8')
                stage.replace(p.txn)
            before = p.raw()
            Path(str(marker) + '.release').touch()
            r = p.result()
            check('concurrent-' + kind + '-refused', r.get('ok') is False and p.raw() == before,
                  r, classification='WINDOWS_DISPOSABLE_PASS')
    for fault in (None,) + STAGES:
        with Probe(exe) as p:
            p.begin()
            before = p.raw()
            ctypes.windll.kernel32.SetFileAttributesW(str(p.txn), 1)
            try:
                if fault:
                    p.request(method='fault', value=fault)
                r = p.request(method='remove')
                check('cleanup-stamp-' + str(fault) + '-observable', r.get('ok') is False
                      and 'could not be persisted' in r.get('error', '') and p.raw() == before,
                      r, classification='SIMULATED_PASS' if fault else 'WINDOWS_DISPOSABLE_PASS')
            finally:
                ctypes.windll.kernel32.SetFileAttributesW(str(p.txn), 128)
    with Probe(exe) as p:
        p.begin()
        before = p.raw()
        ctypes.windll.kernel32.SetFileAttributesW(str(p.txn), 1)
        try:
            r = p.request(method='state', state='ace_verified')
            check('filesystem-replacement-failure-preserves-authority', r.get('ok') is False and p.raw() == before
                  and not list(p.root.glob('grants.txn.json.tmp.*')), r)
        finally:
            ctypes.windll.kernel32.SetFileAttributesW(str(p.txn), 128)
    for raw in (b'null', b'{', b'{"schema":"sai.accounts.helper/txn/99"}'):
        with Probe(exe) as p:
            p.txn.write_bytes(raw)
            r = p.request(method='remove')
            check('invalid-authority-never-cleaned-' + raw.decode(), r.get('ok') is False and p.raw() == raw, r)
    for changes in (dict(txn_id=None), dict(schema='sai.accounts.helper/txn/99')):
        with Probe(exe) as p:
            p.seed(**changes)
            before = p.raw()
            r1 = p.request(method='reconcile')
            r2 = p.request(method='reconcile')
            check('invalid-recovery-repeatably-refuses-' + str(changes), r1.get('ok') is False
                  and r2.get('ok') is False and p.raw() == before, r2)
    with Probe(exe) as p:
        p.begin()
        before = p.raw()
        r = p.begin()
        check('explicit-begin-cannot-discard-pending-authority', r.get('ok') is False and p.raw() == before, r)


def main():
    if os.name != 'nt':
        print('BLOCKED: Windows required')
        return 1
    with tempfile.TemporaryDirectory(prefix='sai-journal-probe-bin-') as tmp:
        exe = Path(tmp) / 'probe.exe'
        compile_probe(exe)
        identity_controls(exe)
        publication_controls(exe)
        rollback_scope_control(exe)
        recovery_scope_control(exe)
        replacement_controls(exe)
    passed = sum(r['passed'] for r in RESULTS)
    print('RESULTS: EXECUTED %d / PASS %d / FAIL %d / SKIP 0' % (len(RESULTS), passed, len(RESULTS)-passed))
    report = os.environ.get('SAI_JOURNAL_TEST_REPORT')
    if report:
        Path(report).write_text(json.dumps(RESULTS, indent=2), encoding='utf-8')
    return int(passed != len(RESULTS))


if __name__ == '__main__':
    sys.exit(main())
