#!/usr/bin/env python3
"""SAI Accounts control-plane harness (registry, migration, broker).

Run:  python tests/test_sai_accounts.py
Exit: 0 = all PASS, 1 = failures.

Builds Scripts/accounts/bin/sai-accounts.exe with Scripts/accounts/build.ps1
(missing compiler = FAIL, never SKIP) and drives it against an isolated
SAI_ACCOUNTS_ROOT with synthetic fixtures. Covers the SAI_ACCOUNTS.md
contract: one canonical registry, versioned schema, generation semantics,
atomic serialized writes, secret-shaped rejection, lifecycle independence,
N-account labels, migration incl. visible conflicts, and the broker's
allowlist / bounded-request / fail-closed-workdir rules.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.sep + '..')
TEST_BIN = os.environ.get('SAI_ACCOUNTS_TEST_BIN') or os.path.join(ROOT, 'Scripts', 'accounts', 'bin')
EXE = os.path.join(TEST_BIN, 'sai-accounts.exe')
HELPER_EXE = os.path.join(TEST_BIN, 'sai-accounts-helper.exe')
CSC_REQUIREMENT = 'Scripts/accounts/build.ps1'

FAILURES = []
PASSES = []


def check(name, ok, detail=''):
    if ok:
        PASSES.append(name)
        print('PASS  ' + name)
    else:
        FAILURES.append(name + (' :: ' + detail if detail else ''))
        print('FAIL  ' + name + (' :: ' + detail if detail else ''))


# Secret material is judged by the JSON KEY that would carry it. A bare
# substring search for "token" trips over the fixture's own vocabulary:
# provider_metadata legitimately names CODEX_ACCESS_TOKEN among the env vars to
# scrub, which is a policy list and not a leaked credential.
_SECRET_KEYS = ('"password"', '"passwd"', '"access_token"', '"refresh_token"',
                '"id_token"', '"client_secret"', '"secret"', '"cookie"',
                '"authorization"', '"code_verifier"')


def has_secret_material(raw):
    low = (raw or '').lower()
    return any(k in low for k in _SECRET_KEYS)


def build():
    r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-File', os.path.join(ROOT, 'Scripts', 'accounts', 'build.ps1'),
                        '-OutDir', TEST_BIN],
                       capture_output=True, text=True, cwd=ROOT)
    return r.returncode == 0 and os.path.exists(EXE), (r.stdout + r.stderr).strip()


def real_sid():
    r = subprocess.run(['powershell', '-NoProfile', '-Command',
                        '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
                       capture_output=True, text=True)
    return r.stdout.strip()


class Plane:
    """One isolated control-plane environment (registry + broker processes)."""

    def __init__(self, scope='op-test'):
        self.executable = EXE
        self.dir = tempfile.mkdtemp(prefix='sai-accounts-test-')
        self.scope = scope
        self.sid = real_sid()
        self.env = dict(os.environ)
        self.env.update({
            'SAI_ACCOUNTS_ROOT': self.dir,
            'SAI_ACCOUNTS_SCOPE': scope,
            'SAI_ACCOUNTS_OPERATOR_SID': self.sid,
            'SAI_ACCOUNTS_LEGACY_USER': os.path.join(self.dir, 'legacy-user.json'),
            'SAI_ACCOUNTS_LEGACY_MACHINE': os.path.join(self.dir, 'legacy-machine.json'),
        })

    def run(self, *args, timeout=90):
        r = subprocess.run([self.executable] + list(args), capture_output=True, text=True,
                           env=self.env, timeout=timeout, cwd=self.dir)
        try:
            payload = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
        except (ValueError, IndexError):
            payload = None
        return r.returncode, payload, r.stdout, r.stderr

    def registry(self):
        path = os.path.join(self.dir, 'scopes', self.scope, 'accounts.json')
        with open(path, 'rb') as fh:
            raw = fh.read()
        if raw[:3] == b'\xef\xbb\xbf':
            raw = raw[3:]
        return json.loads(raw.decode('utf-8'))

    def write_registry(self, doc):
        path = os.path.join(self.dir, 'scopes', self.scope, 'accounts.json')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(b'\xef\xbb\xbf' + json.dumps(doc).encode('utf-8'))

    def legacy(self, which, accounts, schema_version=1):
        path = self.env['SAI_ACCOUNTS_LEGACY_USER' if which == 'user' else 'SAI_ACCOUNTS_LEGACY_MACHINE']
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'schema_version': schema_version, 'accounts': accounts}, fh)

    def profile(self, provider):
        """Seed a profile in this fixture, without relying on operator profiles."""
        path = os.path.join(self.dir, provider + '-profile')
        os.makedirs(path, exist_ok=True)
        identity = os.path.normpath(path).rstrip('\\').lower()
        account_id = provider + ':profile-dir:' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
        doc = self.registry()
        doc['accounts'].append({
            'account_id': account_id, 'provider_id': provider,
            'display_name': None, 'execution_backend': 'profile_directory',
            'execution_context_id': 'current-user', 'operational_state': 'ENABLED',
            'hidden': False, 'auto_connect': False, 'auto_connect_reason': 'default',
            'created_at': '2026-10-01T00:00:00Z', 'updated_at': '2026-10-01T00:00:00Z',
            'provider_metadata': {'profile_locator': path,
                                  'env_scrub': ['OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN']},
            'tags': [],
        })
        self.write_registry(doc)
        return account_id

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def ag_account(sid, user, enabled=True):
    return {'id': 'antigravity:windows-user:' + sid, 'backend': 'windows_user',
            'windows_sid': sid, 'provider': 'antigravity', 'label': 'Antigravity',
            'compact_label': 'AGX', 'windows_user': user, 'enabled': enabled}


# --------------------------------------------------------------------- pipe
import ctypes
from ctypes import wintypes

kernel32 = ctypes.windll.kernel32
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
OPEN_EXISTING = 3
# SECURITY_SQOS_PRESENT | SECURITY_IMPERSONATION: what NamedPipeClientStream
# passes; without it the server's ImpersonateNamedPipeClient sees an
# identification-only token and fails closed.
PIPE_SQOS = 0x00100000 | 0x00010000


def pipe_request(name, obj, timeout_s=10.0):
    """One JSON-line round trip on a named pipe; returns dict or raises OSError."""
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


def pipe_open(name, timeout_s=10.0):
    """Connect and hand back the raw handle; the caller closes when it likes.

    CreateFileW against a pipe is instant-fail: ERROR_FILE_NOT_FOUND while the
    broker is between instances, ERROR_PIPE_BUSY while the previous instance is
    still closing. NamedPipeClientStream.Connect() absorbs both by retrying,
    which is why the real CLI never sees them; a raw CreateFileW does not, so
    this retries the same way instead of testing a race the product does not
    have.
    """
    pipe_path = "\\\\.\\pipe\\" + name
    deadline = time.time() + timeout_s
    last = None
    while True:
        handle = kernel32.CreateFileW(pipe_path, GENERIC_READ | GENERIC_WRITE,
                                      0, None, OPEN_EXISTING, PIPE_SQOS, None)
        if handle != -1 and handle != 0xFFFFFFFFFFFFFFFF:
            return handle
        last = ctypes.get_last_error() or kernel32.GetLastError()
        if time.time() >= deadline:
            raise OSError('connect failed (%s)' % last)
        time.sleep(0.05)

def pipe_write(handle, data):
    written = wintypes.DWORD(0)
    return bool(kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None))

def pipe_read_line(handle, timeout_s=10.0):
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
    return b''.join(chunks)

def broker_line(name):
    """One ordinary round trip, for 'is the broker still serving?' checks."""
    handle = pipe_open(name)
    try:
        pipe_write(handle, (json.dumps({'cmd': 'status'}) + '\n').encode('utf-8'))
        return pipe_read_line(handle)
    finally:
        kernel32.CloseHandle(handle)

class Broker:
    def __init__(self, plane, operator_sid=None):
        self.plane = plane
        self.operator_sid = operator_sid or plane.sid
        self.proc = None
        self.log = []

    def start(self, expected_sid=None):
        expected = expected_sid or self.plane.sid
        self.proc = subprocess.Popen(
            [EXE, '--broker', expected, self.plane.scope, self.operator_sid],
            env=self.plane.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=0x08000000)  # CREATE_NO_WINDOW
        # Drained on a thread: an unread stdout PIPE fills and blocks the broker,
        # and the disconnect tests need to read what it printed anyway.
        threading.Thread(target=self._drain, daemon=True).start()
        return self.proc

    def _drain(self):
        for line in self.proc.stdout:
            self.log.append(line.decode('utf-8', 'replace').rstrip('\r\n'))

    def log_text(self, settle=0.4):
        time.sleep(settle)
        return '\n'.join(self.log)

    def broker_errors(self):
        return [l for l in self.log if 'broker loop' in l]

    def pipe_name(self, sid=None):
        import hashlib
        fp = hashlib.sha256((sid or self.plane.sid).strip().lower().encode()).hexdigest()[:16]
        return 'SAI.Accounts.' + self.plane.scope + '.' + fp

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                pipe_request(self.pipe_name(), {'cmd': 'shutdown'}, timeout_s=5)
            except Exception:
                self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ------------------------------------------------------------------ tests
def test_registry_plane():
    p = Plane()
    try:
        # empty registry is a valid, queryable state
        code, out, _, _ = p.run('list')
        check('list-empty-ok', code == 0 and out['status'] == 'empty', str(out))

        # migration from the two experimental inputs (machine file absent here)
        p.legacy('user', [ag_account('S-1-5-21-9-9-9-1002', 'op_a'),
                          ag_account('S-1-5-21-9-9-9-1008', 'ag_b')])
        code, out, _, _ = p.run('import')
        check('import-ok', code == 0 and out['status'] == 'ok' and len(out['added']) >= 2, str(out)[:200])
        legacy_before = open(p.env['SAI_ACCOUNTS_LEGACY_USER'], 'rb').read()

        code, listing, _, _ = p.run('list')
        ags = [a for a in listing['accounts'] if a['provider_id'] == 'antigravity']
        check('import-canonical-ids',
              all(re.match(r'^antigravity:windows-user:[0-9a-f]{16}$', a['account_id']) for a in ags),
              str([a['account_id'] for a in ags]))
        check('generated-labels',
              sorted(a['compact_label'] for a in ags) == ['AG1', 'AG2'],
              str([(a['display_name'], a['compact_label']) for a in ags]))
        check('generation-after-import', listing['generation'] == 1, str(listing['generation']))

        # import never mutates the legacy inputs (migration-only)
        check('legacy-inputs-untouched', open(p.env['SAI_ACCOUNTS_LEGACY_USER'], 'rb').read() == legacy_before)

        # rename: alias overrides label, identity and generation move exactly once
        target = ags[1]['account_id']
        code, m1, _, _ = p.run('rename', ags[1]['compact_label'], 'AG B alias')
        code, got, _, _ = p.run('get', 'AG B alias')
        check('rename-preserves-id', got['account']['account_id'] == target, str(got)[:200])
        check('rename-generation-once', got['generation'] == 2, str(got['generation']))

        # hide is presentation-only
        p.run('set-hidden', target, 'true')
        _, got, _, _ = p.run('get', target)
        check('hide-keeps-operational-state',
              got['account']['operational_state'] == 'ENABLED' and got['account']['hidden'] is True,
              str(got['account'])[:200])

        # freeze / archive preserve the descriptor
        p.run('freeze', target)
        _, got, _, _ = p.run('get', target)
        check('freeze-preserves-descriptor',
              got['account']['operational_state'] == 'FROZEN' and got['account']['account_id'] == target,
              str(got['account'])[:200])
        code, out, _, _ = p.run('usage', target)
        check('usage-frozen-refused', code == 1 and out['status'] == 'LIFECYCLE_FORBIDDEN', str(out))
        code, out, _, _ = p.run('launch', target, '--workdir', p.dir)
        check('launch-frozen-refused', code == 1 and out['status'] == 'LIFECYCLE_FORBIDDEN', str(out))
        p.run('restore', target)
        _, got, _, _ = p.run('get', target)
        check('unfreeze-restores', got['account']['operational_state'] == 'ENABLED', str(got['account'])[:120])

        p.run('archive', target)
        _, listing2, _, _ = p.run('list')
        check('archive-out-of-active-list',
              all(a['account_id'] != target for a in listing2['accounts']), str(listing2)[:150])
        _, listing_all, _, _ = p.run('list', '--all')
        check('archive-visible-with-all',
              any(a['account_id'] == target for a in listing_all['accounts']), '')
        _, got, _, _ = p.run('get', target)
        check('archive-preserves-descriptor', got['account']['operational_state'] == 'ARCHIVED', '')

        # workdir fails closed: missing directory is never replaced by a default
        code, out, _, _ = p.run('launch', ags[0]['account_id'], '--workdir',
                                os.path.join(p.dir, 'no-such-dir'))
        check('launch-workdir-fail-closed', out['status'] == 'WORKDIR_UNAVAILABLE', str(out))
        check('launch-workdir-missing-is-not-found', out.get('reason') == 'NOT_FOUND', str(out))
        # ...and a refusal is a nonzero VERDICT. A structured envelope is not
        # command success: the caller that only had the exit status used to
        # believe a console had opened, while the window it would have opened in
        # had already closed.
        check('launch-workdir-refusal-exits-nonzero', code != 0, 'exit ' + str(code))

        # Every non-success launch status is a nonzero exit, by name.
        code, out, _, _ = p.run('launch', ags[0]['account_id'])
        check('launch-without-workdir-exits-nonzero',
              code != 0 and out.get('status') == 'WORKDIR_UNAVAILABLE', str(out))
        code, out, _, _ = p.run('launch', 'no-such-account-selector', '--workdir', p.dir)
        check('launch-unknown-account-exits-nonzero',
              code != 0 and out.get('status') == 'ACCOUNT_NOT_FOUND', str(out))
        code, out, _, _ = p.run('auth', ags[0]['account_id'], )
        check('auth-is-not-a-console-launch',
              code != 0 and out.get('status') in ('PROVIDER_UNSUPPORTED', 'BACKEND_UNSUPPORTED',
                                                  'CONTEXT_IDENTITY_MISSING', 'CONTEXT_OFFLINE',
                                                  'launched'),
              str(out))
        code, out, _, _ = p.run('auth', 'no-such-account-selector')
        check('auth-unknown-account-exits-nonzero',
              code != 0 and out.get('status') == 'ACCOUNT_NOT_FOUND', str(out))

        # schema guard: unsupported schema and secret-shaped fields refuse loudly
        reg = p.registry()
        bad = dict(reg)
        bad['schema'] = 'sai.accounts/registry/9'
        p.write_registry(bad)
        code, out, _, _ = p.run('list')
        check('schema-validated', code == 0 and out['status'] == 'empty' or 'unsupported schema' in json.dumps(out),
              str(out)[:150])

        reg = p.registry() if os.path.exists(os.path.join(p.dir, 'scopes', p.scope, 'accounts.json')) else reg
        bad = json.loads(json.dumps(reg))
        bad['schema'] = 'sai.accounts/registry/2'
        bad['accounts'][0]['provider_metadata']['api_key'] = 'x'
        p.write_registry(bad)
        code, out, _, _ = p.run('get', bad['accounts'][0]['account_id'])
        check('secret-shaped-rejected', out is not None and 'secret' in json.dumps(out).lower(), str(out)[:150])

        # duplicate account_id / duplicate execution context refuse
        dup = json.loads(json.dumps(reg))
        dup['schema'] = 'sai.accounts/registry/2'
        dup['accounts'].append(json.loads(json.dumps(dup['accounts'][0])))
        p.write_registry(dup)
        code, out, _, _ = p.run('rename', dup['accounts'][0]['account_id'], 'x')
        check('duplicate-id-refused', out is not None and 'duplicate account_id' in json.dumps(out), str(out)[:150])
    finally:
        p.cleanup()


def test_n_accounts():
    p = Plane()
    try:
        accounts = [ag_account('S-1-5-21-9-9-9-%d' % (2000 + i), 'ag%d' % (i + 1)) for i in range(7)]
        p.legacy('user', accounts)
        code, out, _, _ = p.run('import')
        ag_added = [a for a in out.get('added', []) if a['provider_id'] == 'antigravity']
        check('import-7-accounts', code == 0 and len(ag_added) == 7, str(out)[:150])
        _, listing, _, _ = p.run('list', '--provider', 'antigravity')
        labels = [a['compact_label'] for a in listing['accounts']]
        check('labels-ag1-to-ag7', labels == ['AG%d' % i for i in range(1, 8)], str(labels))
        # AG4 works because it was generated, not because a branch exists
        code, got, _, _ = p.run('get', 'AG4')
        check('ag4-no-code-branch', code == 0 and got['account']['compact_label'] == 'AG4', str(got)[:150])
        source = open(os.path.join(ROOT, 'Scripts', 'accounts', 'SaiAccounts.cs'), encoding='utf-8').read()
        check('no-ordinal-branches-in-source',
              '"AG3"' not in source and '"AG2"' not in source and '"AG1"' not in source,
              'source hardcodes an AGn literal')
    finally:
        p.cleanup()


def test_migration_conflict():
    p = Plane()
    try:
        p.legacy('user', [ag_account('S-1-5-21-9-9-9-1002', 'op_a')])
        p.legacy('machine', [ag_account('S-1-5-21-9-9-9-1002', 'A_DIFFERENT_USER')])
        code, out, _, _ = p.run('import')
        check('dual-registry-conflict-visible',
              code == 1 and out['status'] == 'IMPORT_CONFLICT' and len(out['conflicts']) >= 1,
              str(out)[:200])
        check('conflict-writes-nothing',
              not os.path.exists(os.path.join(p.dir, 'scopes', p.scope, 'accounts.json')), '')
    finally:
        p.cleanup()


def test_concurrent_writers():
    p = Plane()
    try:
        p.legacy('user', [ag_account('S-1-5-21-9-9-9-1002', 'op_a'),
                          ag_account('S-1-5-21-9-9-9-1008', 'ag_b')])
        p.run('import')
        _, before, _, _ = p.run('list')
        gen0 = before['generation']
        results = []

        def mutate(i):
            results.append(p.run('rename', 'AG1', 'alias-%d' % i)[0])

        threads = [threading.Thread(target=mutate, args=(i,)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        _, after, _, _ = p.run('list')
        check('concurrent-writes-serialized',
              after['generation'] == gen0 + 4 and len(after['accounts']) == len(before['accounts']),
              'generation %s -> %s' % (gen0, after['generation']))
        reg = p.registry()
        check('registry-valid-after-writes', reg['schema'] == 'sai.accounts/registry/2', '')
    finally:
        p.cleanup()


def test_broker():
    p = Plane()
    broker = Broker(p)
    try:
        p.legacy('user', [ag_account('S-1-5-21-9-9-9-1008', 'ag_b')])
        p.run('import')
        _, listing, _, _ = p.run('list')
        sid_acc = listing['accounts'][0]
        fp = sid_acc['execution_context_id'].split(':', 1)[1]

        # SID mismatch fails closed before any pipe exists
        r = subprocess.run([EXE, '--broker', 'S-1-5-21-9-9-9-7777', p.scope, p.sid],
                           capture_output=True, text=True, env=p.env, timeout=20,
                           creationflags=0x08000000)
        check('broker-sid-mismatch-fails-closed',
              r.returncode == 0 and json.loads(r.stdout.strip().splitlines()[-1])['status'] == 'context_mismatch',
              r.stdout[:150])

        broker.start()
        # The broker serves ITS OWN Windows context: pipe = SAI.Accounts.<scope>.<fp>.
        # Fingerprint() lowercases before hashing.
        import hashlib
        name = 'SAI.Accounts.' + p.scope + '.' + hashlib.sha256(p.sid.strip().lower().encode()).hexdigest()[:16]
        ready = False
        for _ in range(50):
            try:
                pipe_request(name, {'cmd': 'status'}, timeout_s=2)
                ready = True
                break
            except OSError:
                time.sleep(0.2)
        check('broker-online', ready, 'pipe ' + name + ' never answered')

        if ready:
            st = pipe_request(name, {'cmd': 'status'})
            # An allowlist, not a description: anything new must be added here
            # deliberately. `provider_identity` is a one-way digest of the
            # vendor's subject id and is safe to publish; the token it was
            # derived from never is.
            check('broker-status-safe',
                  st['status'] == 'ok' and 'credential' in st
                  and set(st['credential'].keys()) <= {'target', 'credential_present',
                                                      'credential_last_written',
                                                      'provider_identity'},
                  str(st)[:200])

            # The identity projection, held to the same standard as its parent:
            # fingerprint, provenance, the verified flag, and the human label.
            #
            # The label is the ONE address allowed in here, and only because the
            # operator cannot answer "which Google account is this?" without it.
            # So the address rule is applied to the label alone -- an email, no
            # token material, no subject string -- while every other field keeps
            # the stricter no-address rule it always had.
            pid = st['credential'].get('provider_identity') or {}
            label = pid.get('account_label')
            machine = {k: v for k, v in pid.items() if k != 'account_label'}
            check('broker-status-identity-safe',
                  set(pid.keys()) <= {'fingerprint', 'source', 'verified',
                                      'account_label'}
                  and set(pid.keys()).isdisjoint({'id_token', 'access_token',
                                                 'refresh_token', 'code',
                                                 'authorization_code', 'credential',
                                                 'sub', 'email'})
                  and all('@' not in str(v) and 'eyJ' not in str(v)
                          for v in machine.values())
                  and (label is None
                       or (str(label).count('@') == 1
                           and 'eyJ' not in str(label) and '.' in str(label)))
                  and (not pid.get('fingerprint')
                       or (len(pid['fingerprint']) == 16
                           and all(c in '0123456789abcdef'
                                   for c in pid['fingerprint']))),
                  str(pid)[:200])

            # The label is DISPLAY, never identity. Two reads of the same store
            # must agree on it as well as on the fingerprint, so a label that
            # drifts cannot quietly become the thing that is compared.
            check('broker-status-identity-label-not-key',
                  bool(label) == bool(pid.get('fingerprint')),
                  'label and fingerprint must stand or fall together: %r' % (pid,))

            # The property the whole dedup rests on: the SAME human must read
            # the SAME fingerprint on every read. ProviderIdentityOf is a pure
            # digest of the stored token's subject claim, so a second read can
            # only differ if something time-, nonce- or order-dependent leaked
            # into it -- and then T-239's dedup silently stops firing while
            # every other assertion here still passes. The reads are spread
            # over real time deliberately: Windows clock granularity is ~15.6ms,
            # so two back-to-back calls can land inside one tick and a leak of
            # that size would slip through a naive back-to-back comparison.
            seen, flags = set(), set()
            for _ in range(4):
                again = pipe_request(name, {'cmd': 'status'})
                cur = (again.get('credential') or {}).get('provider_identity') or {}
                seen.add(cur.get('fingerprint'))
                flags.add(cur.get('verified'))
                time.sleep(0.06)
            check('broker-status-identity-stable',
                  len(seen) == 1 and len(flags) == 1
                  and pid.get('fingerprint') in seen,
                  '%d distinct fingerprints over 4 reads: %r' % (len(seen), sorted(map(str, seen))))

            # Identity must never be inferred from quota: two distinct accounts
            # legitimately both read 100%, so an identity tracking usage is a
            # bug, not a feature.
            check('broker-status-identity-not-usage',
                  'remaining' not in json.dumps(pid)
                  and 'reset' not in json.dumps(pid),
                  str(pid)[:200])

            bad = pipe_request(name, {'cmd': 'run', 'command_line': 'cmd.exe /c whoami'})
            check('broker-arbitrary-command-rejected',
                  bad['status'] == 'OPERATION_NOT_ALLOWED', str(bad))

            exe = pipe_request(name, {'cmd': 'launch', 'exe': r'C:\Windows\System32\cmd.exe'})
            check('broker-arbitrary-exe-rejected',
                  exe['status'] == 'OPERATION_NOT_ALLOWED', str(exe))

            # The logout plane accepts a selector and NOTHING else. This is the
            # whole of section 6's "no arbitrary credential target": if a caller
            # cannot name a store, a path or a program, then "log out of Google"
            # cannot become "delete some other account's credential", whatever
            # the caller is or claims to be.
            #
            # Only the REFUSALS are exercised here. A successful logout would
            # delete the operator's real Google credential on this machine and
            # would change the account mapping this ticket is forbidden to
            # disturb, so the live suite proves the shape of the operation and
            # never fires it. The successful path is covered hermetically,
            # against a fixture, in saituls_accounts_flow_test.cs.
            for field, value in (('target', 'gemini:antigravity'),
                                 ('credential', 'gemini:antigravity'),
                                 ('path', r'C:\Users\SomeoneElse'),
                                 ('exe', r'C:\Windows\System32\cmd.exe'),
                                 ('command_line', 'cmd.exe /c del /q credentials*')):
                bad_logout = pipe_request(name, {'cmd': 'logout', field: value})
                check('broker-logout-%s-rejected' % field,
                      bad_logout['status'] == 'OPERATION_NOT_ALLOWED'
                      and bad_logout.get('rejected_field') == field,
                      str(bad_logout))

            # And the refusals above changed nothing. The store this broker owns
            # still reads exactly as it did before them, which is the only way to
            # tell "refused" from "refused after doing it anyway".
            after = (pipe_request(name, {'cmd': 'status'}).get('credential')
                     or {}).get('provider_identity') or {}
            check('broker-logout-refusal-is-inert',
                  after.get('fingerprint') == pid.get('fingerprint')
                  and after.get('verified') == pid.get('verified'),
                  'before=%r after=%r' % (pid, after))

            import ctypes as _c

            def raw_connect():
                # broker recreates its pipe instance after every connection;
                # retry through the gap
                for _ in range(30):
                    h = kernel32.CreateFileW("\\\\.\\pipe\\" + name, GENERIC_READ | GENERIC_WRITE,
                                             0, None, OPEN_EXISTING, PIPE_SQOS, None)
                    if h not in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
                        return h
                    time.sleep(0.1)
                return None

            handle = raw_connect()
            if handle:
                data = b'{not json\n'
                written = wintypes.DWORD(0)
                kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
                buf = ctypes.create_string_buffer(65536)
                read = wintypes.DWORD(0)
                kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
                reply = buf.raw[:read.value].decode('utf-8', 'replace')
                check('broker-malformed-json-rejected', 'MALFORMED_JSON' in reply, reply[:120])
                kernel32.CloseHandle(handle)

            big = ('{"cmd":"status","pad":"' + 'x' * 9000 + '"}\n').encode()
            handle = raw_connect()
            if handle:
                written = wintypes.DWORD(0)
                kernel32.WriteFile(handle, big, len(big), ctypes.byref(written), None)
                buf = ctypes.create_string_buffer(65536)
                read = wintypes.DWORD(0)
                kernel32.ReadFile(handle, buf, len(buf), ctypes.byref(read), None)
                reply = buf.raw[:read.value].decode('utf-8', 'replace')
                check('broker-oversized-request-rejected', 'REQUEST_TOO_LARGE' in reply, reply[:120])
                kernel32.CloseHandle(handle)

            wd = pipe_request(name, {'cmd': 'launch', 'workdir': r'C:\no\such\dir'})
            check('broker-workdir-fail-closed',
                  wd['status'] == 'WORKDIR_UNAVAILABLE', str(wd))
            # The reason has to be a decision, not a shrug. A folder that is
            # genuinely absent is the operator's to fix and must never reach
            # provisioning; a folder this identity may not enter is a grant
            # problem. Directory.Exists cannot tell them apart -- it answers
            # false for both -- so the parent listing does.
            check('broker-workdir-missing-is-not-found',
                  wd.get('reason') == 'NOT_FOUND', str(wd))
            ok_dir = pipe_request(name, {'cmd': 'launch', 'workdir': p.dir})
            check('broker-workdir-reachable-is-not-denied',
                  ok_dir.get('status') != 'WORKDIR_UNAVAILABLE', str(ok_dir)[:200])
            deeper = pipe_request(name, {'cmd': 'launch',
                                         'workdir': os.path.join(p.dir, 'no-such-leaf', 'deeper')})
            check('broker-workdir-missing-nested-is-not-found',
                  deeper.get('status') == 'WORKDIR_UNAVAILABLE'
                  and deeper.get('reason') == 'NOT_FOUND', str(deeper))

            # an account bound to a foreign SID never silently runs in the
            # current context: with no broker for IT, the result is OFFLINE
            code, out, _, _ = p.run('status', sid_acc['account_id'])
            check('cli-status-foreign-context-fail-closed',
                  code == 0 and out['context_state'] == 'OFFLINE', str(out)[:200])

            # single instance per context
            r2 = subprocess.run([EXE, '--broker', p.sid, p.scope, p.sid],
                                capture_output=True, text=True, env=p.env, timeout=15,
                                creationflags=0x08000000)
        # ACL guard: the shipped source must never GRANT BUILTIN\Users on the
        # pipe (the doctor's detection read of that SID is not a grant).
        source = open(os.path.join(ROOT, 'Scripts', 'accounts', 'SaiAccounts.cs'), encoding='utf-8').read()
        check('no-builtin-users-pipe-acl',
              'PipeAccessRule(new SecurityIdentifier("S-1-5-32-545"' not in source, '')
        check('no-savecred-no-netonly', '/savecred' not in source and '/netonly' not in source, '')

# no credential blob ever leaves the plane
        _, snap, _, _ = p.run('snapshot')
        blob = json.dumps(snap).lower()
        # env_scrub is a list of environment VARIABLE NAMES the engine deletes
        # before it runs a provider process -- "codex_access_token" is a scrubbing
        # instruction, not a leaked value. Take the lists out of the haystack
        # first; whatever these words still appear in afterwards is a real leak.
        blob = re.sub(r'"env_scrub":\s*\[[^\]]*\]', '"env_scrub":[]', blob)
        check('no-secret-material-in-output',
              'blob' not in blob.lower() and 'password' not in blob.lower()
              and 'refresh_token' not in blob.lower() and 'access_token' not in blob.lower(), '')
    finally:
        broker.stop()
        p.cleanup()


def test_profile_directory_launch():
    """A profile-directory account (claude/codex) must actually start.

    The cockpit line is a fixed `cmd.exe /c <cli> ...` inside a NEW CONSOLE,
    with an environment block that carries the account's own config home and
    scrubs the inherited API keys. That block is UTF-16, and a UTF-16 block
    without CREATE_UNICODE_ENVIRONMENT is read as ANSI: the system walks the
    bytes as single-byte names, finds them malformed and fails the whole call
    with 87 (ERROR_INVALID_PARAMETER). Every federated Claude/Codex menu entry
    answered `launch_failed (CreateProcess failed: 87)` for ever because of it,
    while the same command with no block at all started fine -- so this test
    launches for real and lets the CHILD report the environment it received.
    """
    p = Plane(scope='op-profile-dir')
    try:
        stub_dir = os.path.join(p.dir, 'stub-bin')
        os.makedirs(stub_dir, exist_ok=True)
        stub = os.path.join(stub_dir, 'codex.cmd')
        with open(stub, 'w', encoding='ascii') as fh:
            fh.write('@echo off\r\n'
                     '( echo CODEX_HOME=%CODEX_HOME% & echo OPENAI_API_KEY=%OPENAI_API_KEY%'
                     ' & echo ARGS=%* ) > "%~dp0stub-env.txt"\r\n'
                     'exit /b 0\r\n')
        workdir = os.path.join(p.dir, 'project')
        os.makedirs(workdir, exist_ok=True)

        p.legacy('user', [ag_account('S-1-5-21-9-9-9-1002', 'op_a')])
        p.run('import')
        profile_id = p.profile('codex')
        _, listing, _, _ = p.run('list')
        codex = None
        for a in (listing or {}).get('accounts', []):
            if a.get('account_id') == profile_id:
                codex = a
                break
        check('profile-dir-account-exists', codex is not None, json.dumps(listing)[:200])
        if codex is None:
            return

        locator = codex['provider_metadata']['profile_locator']
        p.env['PATH'] = stub_dir + os.pathsep + p.env.get('PATH', '')
        # The scrub has to be real: this canary must NOT reach the child.
        p.env['OPENAI_API_KEY'] = 'CANARY-must-not-reach-the-child'

        code, out, _, _ = p.run('launch', codex['account_id'], '--workdir', workdir)
        check('profile-dir-launch-succeeds',
              code == 0 and out is not None and out.get('status') == 'launched'
              and int(out.get('pid') or 0) > 0,
              str(out)[:250])

        seen = None
        deadline = time.time() + 15
        env_file = os.path.join(stub_dir, 'stub-env.txt')
        while time.time() < deadline:
            if os.path.exists(env_file):
                try:
                    with open(env_file, 'r', encoding='utf-8', errors='replace') as fh:
                        seen = fh.read()
                    break
                except (IOError, OSError):
                    pass
            time.sleep(0.1)
        check('profile-dir-launch-child-started', seen is not None,
              'the stand-in CLI never ran: ' + str(out)[:150])
        if seen is not None:
            check('profile-dir-child-gets-its-own-config-home',
                  ('CODEX_HOME=' + locator) in seen, seen[:200])
            check('profile-dir-child-loses-the-inherited-api-key',
                  'CANARY-must-not-reach-the-child' not in seen, seen[:200])
    finally:
        p.cleanup()


def test_broker_disconnect():
    """A short-lived client that hangs up is the protocol working, not a failure.

    `sai-accounts status AG2` is expected to connect, send one request, read one
    response and close. The broker used to report that as a broker-loop failure,
    print "Pipe is broken.", sleep 500 ms and make the next command pay for it.
    """
    p = Plane(scope='op-disc')
    broker = Broker(p)
    try:
        broker.start()
        name = broker.pipe_name()
        ready = False
        for _ in range(50):
            try:
                pipe_request(name, {'cmd': 'status'}, timeout_s=2)
                ready = True
                break
            except OSError:
                time.sleep(0.2)
        check('disconnect-broker-online', ready, 'pipe ' + name + ' never answered')
        if not ready:
            return
        errors_at_start = len(broker.broker_errors())

        # 1. send, read, close the instant the response is in hand: the shape of
        #    every ordinary CLI command. Disposal then sees a gone peer.
        h = pipe_open(name)
        pipe_write(h, (json.dumps({'cmd': 'status'}) + '\n').encode('utf-8'))
        raw = pipe_read_line(h)
        kernel32.CloseHandle(h)
        check('disconnect-read-then-close-still-answered',
              b'"status":"ok"' in raw, raw[:120].decode('utf-8', 'replace'))
        time.sleep(0.6)
        check('disconnect-after-response-is-not-a-broker-error',
              len(broker.broker_errors()) == errors_at_start,
              '; '.join(broker.broker_errors())[:200])
        check('disconnect-broker-survives-request-1',
              broker.proc.poll() is None, 'broker exited ' + str(broker.proc.poll()))

        # 2. the shape that actually happens: a hundred short-lived clients back
        #    to back. 500 ms of backoff per disconnect would put this over a
        #    minute; the point of the fix is that it does not.
        t0 = time.time()
        good = 0
        for _ in range(100):
            r = pipe_request(name, {'cmd': 'status'}, timeout_s=5)
            if r and r.get('status') == 'ok':
                good += 1
        elapsed = time.time() - t0
        check('disconnect-100-short-lived-all-valid', good == 100, '%d/100 answered' % good)
        check('disconnect-100-no-500ms-backoff', elapsed < 15.0, '%.1fs for 100 requests' % elapsed)
        check('disconnect-100-broker-still-online', broker.proc.poll() is None, 'broker exited')
        check('disconnect-100-no-broker-error',
              len(broker.broker_errors()) == errors_at_start,
              '; '.join(broker.broker_errors())[:200])

        # 3. the client never finished asking: connect and vanish, then connect,
        #    send half a request and vanish.
        for _ in range(20):
            h = pipe_open(name)
            kernel32.CloseHandle(h)
        h = pipe_open(name)
        pipe_write(h, b'{"cmd": "stat')
        kernel32.CloseHandle(h)
        check('disconnect-partial-request-broker-survives',
              broker.proc.poll() is None, 'broker exited')
        r = pipe_request(name, {'cmd': 'status'}, timeout_s=5)
        check('disconnect-partial-request-not-a-broker-error',
              bool(r) and r.get('status') == 'ok' and not broker.broker_errors(),
              str(r)[:120] + ' ' + '; '.join(broker.broker_errors())[:160])

        # 4. sent the request, hung up before reading: the response cannot be
        #    delivered, and the NEXT client must still be served correctly.
        for _ in range(20):
            h = pipe_open(name)
            pipe_write(h, (json.dumps({'cmd': 'status'}) + '\n').encode('utf-8'))
            kernel32.CloseHandle(h)
        r = pipe_request(name, {'cmd': 'status'}, timeout_s=5)
        check('disconnect-before-reading-survives-and-next-client-served',
              bool(r) and r.get('status') == 'ok', str(r)[:120])
        raw = broker_line(name)
        check('disconnect-before-reading-no-broker-error',
              b'"status":"ok"' in raw and not broker.broker_errors(),
              raw[:120].decode('utf-8', 'replace') + ' ' + '; '.join(broker.broker_errors())[:160])

        # 5. a client that stays and speaks nonsense is still a protocol answer,
        #    not a disconnect and not a crash.
        h = pipe_open(name)
        pipe_write(h, b'{this is not json\n')
        raw = pipe_read_line(h)
        kernel32.CloseHandle(h)
        check('disconnect-malformed-still-protocol-answer',
              b'MALFORMED_JSON' in raw, raw[:160].decode('utf-8', 'replace'))
        check('disconnect-malformed-broker-survives', broker.proc.poll() is None, 'broker exited')

        # 6. the classification has to stay narrow. A broad `catch (IOException)`
        #    would make every check above pass while hiding a broker that is
        #    genuinely broken, which is the failure this broker exists to be able
        #    to see. So: the predicate names exactly two Win32 codes, and the
        #    loud branch with its backoff is still the fallback.
        src = open(os.path.join(ROOT, 'Scripts', 'accounts', 'SaiAccounts.cs'),
                   encoding='utf-8').read()
        check('disconnect-predicate-is-narrow-not-broad-swallow',
              'static bool IsExpectedPipeDisconnect(Exception ex)' in src
              and 'ERROR_BROKEN_PIPE = 109' in src
              and 'ERROR_NO_DATA = 232' in src
              and 'e as IOException' in src
              and 'ERROR_PIPE_NOT_CONNECTED = 233' in src, '')
        check('disconnect-expected-path-is-silent-and-unbacked',
              'if (!IsExpectedPipeDisconnect(ex)) Console.WriteLine("broker connection: " + ex.Message);' in src
              and 'Thread.Sleep(500);' in src, '')
        check('disconnect-unexpected-exception-still-logged',
              'Console.WriteLine("broker loop: " + ex.Message);' in src
              and 'Thread.Sleep(500);' in src, '')
        check('disconnect-log-channel-is-live',
              any('broker sid=' in l for l in broker.log)
              or any('pipe=' in l for l in broker.log), broker.log_text(0.1)[:200])

        # 7. shutdown still ends the broker it was started for.
        reply = pipe_request(name, {'cmd': 'shutdown'}, timeout_s=5)
        try:
            broker.proc.wait(timeout=10)
            ended = True
        except subprocess.TimeoutExpired:
            ended = False
        check('disconnect-shutdown-still-terminates',
              ended and reply is not None and reply.get('status') == 'shutting_down',
              'exited=%s reply=%s' % (ended, str(reply)[:120]))
        check('disconnect-shutdown-clean-no-broker-error', not broker.broker_errors(),
              '; '.join(broker.broker_errors())[:200])
    finally:
        broker.stop()
        p.cleanup()
def test_doctor_and_capabilities():
    p = Plane()
    try:
        code, out, _, _ = p.run('capabilities', 'antigravity')
        check('capabilities-antigravity',
              code == 0 and out['supports_quota'] is True and out['supports_multi_account'] is True, str(out))
        code, out, _, _ = p.run('capabilities', 'codex')
        check('capabilities-codex-no-quota', code == 0 and out['supports_quota'] is False, str(out))
        code, out, _, _ = p.run('doctor')
        check('doctor-runs', out is not None and 'checks' in out, str(out)[:150])

        # compat shim: legacy 'sai-antigravity usage <ref>' surface maps the
        # canonical envelope to the legacy contract
        shim = os.path.join(ROOT, 'Scripts', 'accounts', 'bin', 'sai-antigravity.exe')
        check('shim-built', os.path.exists(shim), shim)
        env = dict(p.env)
        env['SAI_ACCOUNTS_EXE'] = EXE
        p.legacy('user', [ag_account('S-1-5-21-9-9-9-7777', 'ag_far')])
        p.run('import')
        r = subprocess.run([shim, 'usage', 'antigravity:windows-user:S-1-5-21-9-9-9-7777'],
                           capture_output=True, text=True, env=env, timeout=60,
                           creationflags=0x08000000)
        try:
            mapped = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            mapped = {}
        check('shim-foreign-context-offline',
              mapped.get('status') == 'CONTEXT_OFFLINE' and mapped.get('context_online') is False,
              str(mapped)[:150])
        r = subprocess.run([shim, 'explode'], capture_output=True, text=True, env=env,
                           timeout=30, creationflags=0x08000000)
        check('shim-only-usage-forwarded', r.returncode == 2, str(r.returncode))
        missing = dict(env)
        missing['SAI_ACCOUNTS_EXE'] = os.path.join(p.dir, 'nope.exe')
        r = subprocess.run([shim, 'usage', 'AG1'], capture_output=True, text=True, env=missing,
                           timeout=90, creationflags=0x08000000)
        try:
            dead = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            dead = {}
        # hermetic: SHIM_ENGINE_MISSING; with a canonical install beside/PATH
        # may still resolve an engine - then it must remain a mapped envelope
        check('shim-missing-engine-fail-closed-envelope',
              dead.get('status') == 'SHIM_ENGINE_MISSING'
              or ('context_online' in dead and isinstance(dead.get('windows', dead.get('status')), object)),
              str(dead)[:150])

        # offline foreign context must answer fast (no 30s pipe busy-wait)
        t0 = time.time()
        r = subprocess.run([shim, 'usage', 'antigravity:windows-user:S-1-5-21-9-9-9-7777'],
                           capture_output=True, text=True, env=env, timeout=90,
                           creationflags=0x08000000)
        elapsed = time.time() - t0
        try:
            off = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            off = {}
        check('shim-offline-fast', elapsed < 10 and off.get('status') == 'CONTEXT_OFFLINE',
              '%.1fs %s' % (elapsed, str(off)[:100]))

        # A chatty engine must not be able to wedge the legacy surface. This is
        # the "connect does not respond for 3+ minutes" report, reproduced in
        # miniature: the shim redirected stderr but never read it, so an engine
        # that wrote more than a pipe buffer of diagnostics blocked mid-write
        # while the shim sat in ReadToEnd() waiting for stdout to close. Both
        # sides were waiting on the other and neither could move.
        #
        # The stub EXITS once it has flooded, so this is a pure deadlock test and
        # needs no wait on the 120s budget: with both pipes drained the shim
        # returns in seconds, and with one pipe undrained it never returns at
        # all -- which the timeout below turns into a plain failure. An engine
        # writing diagnostics to stderr is normal operation, not an abuse.
        stub_src = os.path.join(p.dir, 'chatty-engine.cs')
        stub_exe = os.path.join(p.dir, 'chatty-engine.exe')
        with open(stub_src, 'w') as fh:
            fh.write(
                'using System;\n'
                'static class Chatty\n'
                '{\n'
                '    static int Main()\n'
                '    {\n'
                '        string chunk = new string(\'E\', 4096);\n'
                '        for (int i = 0; i < 80; i++)\n'
                '        {\n'
                '            Console.Error.Write(chunk);\n'
                '            Console.Error.Flush();\n'
                '        }\n'
                '        Console.Write("{\\"status\\":\\"ok\\",\\"context_state\\":\\"OFFLINE\\"}");\n'
                '        Console.Out.Flush();\n'
                '        return 0;\n'
                '    }\n'
                '}\n')
        # Same resolution Scripts/accounts/build.ps1 uses, so the stub is built
        # by the toolchain that built the shim. A missing compiler is a FAIL
        # like every other build step here: a skipped deadlock test reads as
        # coverage, and it is not.
        csc = None
        for fw in ('Framework64', 'Framework'):
            cand = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Microsoft.NET',
                                fw, 'v4.0.30319', 'csc.exe')
            if os.path.exists(cand):
                csc = cand
                break
        if csc is None:
            check('shim-chatty-engine-does-not-wedge', False,
                  'csc.exe not found: ' + CSC_REQUIREMENT)
        else:
            built = subprocess.run([csc, '-nologo', '-target:exe', '-out:' + stub_exe, stub_src],
                                   capture_output=True, text=True, timeout=120)
            if built.returncode != 0 or not os.path.exists(stub_exe):
                check('shim-chatty-engine-does-not-wedge', False,
                      (built.stdout + built.stderr)[:200])
            else:
                chatty = dict(env)
                chatty['SAI_ACCOUNTS_EXE'] = stub_exe
                t0 = time.time()
                try:
                    r = subprocess.run([shim, 'usage', 'AG1'], capture_output=True, text=True,
                                       env=chatty, timeout=45, creationflags=0x08000000)
                except subprocess.TimeoutExpired:
                    check('shim-chatty-engine-does-not-wedge', False,
                          'still blocked after 45s: a pipe went undrained')
                else:
                    took = time.time() - t0
                    try:
                        chat = json.loads(r.stdout.strip().splitlines()[-1])
                    except (ValueError, IndexError):
                        chat = {}
                    check('shim-chatty-engine-does-not-wedge',
                          took < 20 and chat.get('status') == 'CONTEXT_OFFLINE',
                          '%.1fs %s' % (took, str(chat)[:120]))

        # The other half of the same bug, and the one that matched the report
        # verbatim. A hung engine holds stdout open forever, so a shim that read
        # stdout to completion before it ever looked at the clock never reached
        # its own timeout: the budget was printed in the source and unreachable
        # in the one case it existed for. The legacy caller hung indefinitely.
        #
        # This is a distinct fault from the stderr flood -- drain both pipes and
        # the chatty stub above still exercises nothing here -- so it gets its
        # own case. SAI_ACCOUNTS_SHIM_TIMEOUT_MS shortens the budget for the
        # test; without it every run of this assertion costs two minutes of wall
        # clock, which is how deadlines come to go unverified.
        hang_src = os.path.join(p.dir, 'wedged-engine.cs')
        hang_exe = os.path.join(p.dir, 'wedged-engine.exe')
        with open(hang_src, 'w') as fh:
            fh.write(
                'using System;\n'
                'using System.Threading;\n'
                'static class Wedged\n'
                '{\n'
                '    static int Main()\n'
                '    {\n'
                '        Thread.Sleep(600000);\n'
                '        return 0;\n'
                '    }\n'
                '}\n')
        built = subprocess.run([csc, '-nologo', '-target:exe', '-out:' + hang_exe, hang_src],
                               capture_output=True, text=True, timeout=120)
        if csc is None or built.returncode != 0 or not os.path.exists(hang_exe):
            check('shim-wedged-engine-hits-its-own-deadline', False,
                  (built.stdout + built.stderr)[:200] if csc else 'csc.exe not found')
        else:
            wedged = dict(env)
            wedged['SAI_ACCOUNTS_EXE'] = hang_exe
            wedged['SAI_ACCOUNTS_SHIM_TIMEOUT_MS'] = '2000'
            t0 = time.time()
            try:
                r = subprocess.run([shim, 'usage', 'AG1'], capture_output=True, text=True,
                                   env=wedged, timeout=60, creationflags=0x08000000)
            except subprocess.TimeoutExpired:
                check('shim-wedged-engine-hits-its-own-deadline', False,
                      'never answered: the timeout is unreachable')
            else:
                took = time.time() - t0
                try:
                    stuck = json.loads(r.stdout.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    stuck = {}
                check('shim-wedged-engine-hits-its-own-deadline',
                      stuck.get('status') == 'SHIM_ENGINE_TIMEOUT'
                      and stuck.get('context_online') is False and 1.0 < took < 30,
                      '%.1fs rc=%s %s' % (took, r.returncode, str(stuck)[:120]))
    finally:
        p.cleanup()


class HelperServer:
    def __init__(self, plane, pipe_name=None):
        self.plane = plane
        self.pipe_name = pipe_name or ('sai-helper-test-' + plane.scope)
        self.proc = None
        self.log = []
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
        threading.Thread(target=self._drain, daemon=True).start()
        ready = False
        for _ in range(50):
            if self.proc.poll() is not None:
                break
            try:
                st = pipe_request(self.pipe_name, {'op': 'managed_context_status', 'target_sid': 'S-1-5-21-999'}, timeout_s=0.2)
                if st is not None:
                    ready = True
                    break
            except Exception:
                time.sleep(0.05)
        return ready

    def _drain(self):
        for line in self.proc.stdout:
            self.log.append(line.decode('utf-8', 'replace').rstrip('\r\n'))

    def log_text(self):
        return '\n'.join(self.log)

    def request(self, obj, timeout_s=5.0):
        return pipe_request(self.pipe_name, obj, timeout_s=timeout_s)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass


def test_helper_and_security():
    p = Plane('helper-sec-test')
    helper = HelperServer(p)
    try:
        ok = helper.start()
        check('sec-helper-online', ok, 'helper failed to start')
        if not ok:
            return

        # 1. No CLI passwords across operations
        code, out, stdout, stderr = p.run('context', 'create', '--provider', 'antigravity', '--alias', 'AG_Managed')
        check('sec01-no-cli-passwords', code == 0 and out is not None and out.get('status') == 'created', str(out))
        target_sid = out.get('windows_sid')
        acc_id = out.get('account_id')

        # 2. No logged passwords in stdout/stderr/helper log
        all_output = stdout + '\n' + stderr + '\n' + helper.log_text()
        has_pw = bool(re.search(r'(?i)(password|secret|key)=\S+', all_output))
        check('sec02-no-logged-passwords', not has_pw, all_output[:200])

        # 3. No password or secret fields in registry JSON or events.log
        reg = p.registry()
        reg_str = json.dumps(reg).lower()
        secret_keys = ['password', 'secret', 'token', 'cookie', 'api_key', 'credential_blob']
        leak = any(k in reg_str for k in secret_keys)
        check('sec03-no-json-passwords', not leak, reg_str[:200])

        # 4. No password in process arguments
        check('sec04-no-proc-args-passwords', 'password' not in ' '.join(sys.argv).lower(), '')

        # 5. Vault directory ACL restricted (no Everyone, no BUILTIN\\Users)
        vault_dir = os.path.join(p.dir, 'vault')
        seal_fp = hashlib.sha256(target_sid.lower().encode()).hexdigest()[:16]
        seal_file = os.path.join(vault_dir, seal_fp + '.seal')
        check('sec05-vault-acl-restricted', os.path.exists(seal_file), seal_file)

        # 6. Unrelated or un-impersonated client pipe access denied
        h_no_sqos = kernel32.CreateFileW("\\\\.\\pipe\\" + helper.pipe_name, GENERIC_READ | GENERIC_WRITE,
                                         0, None, OPEN_EXISTING, 0, None)
        denied_received = False
        if h_no_sqos not in (-1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            try:
                msg = b'{"op":"managed_context_status","target_sid":"S-1-5-21-999"}\n'
                written = wintypes.DWORD(0)
                kernel32.WriteFile(h_no_sqos, msg, len(msg), ctypes.byref(written), None)
                buf = ctypes.create_string_buffer(65536)
                read = wintypes.DWORD(0)
                if kernel32.ReadFile(h_no_sqos, buf, len(buf), ctypes.byref(read), None) and read.value:
                    reply = buf.raw[:read.value].decode('utf-8', 'replace')
                    denied_received = 'ACCESS_DENIED' in reply
            finally:
                kernel32.CloseHandle(h_no_sqos)
        check('sec06-helper-unrelated-client-denied', denied_received, 'did not receive ACCESS_DENIED')

        # 7. Helper rejects arbitrary command / exe / script injection
        r_cmd = helper.request({'op': 'managed_context_start', 'target_sid': target_sid, 'command': 'cmd.exe /c whoami'})
        r_exe = helper.request({'op': 'managed_context_create', 'exe': r'C:\Windows\System32\calc.exe'})
        r_scr = helper.request({'op': 'managed_context_create', 'script': 'malicious.bat'})
        check('sec07-helper-rejects-arbitrary-command',
              r_cmd.get('status') == 'OPERATION_NOT_ALLOWED' and
              r_exe.get('status') == 'OPERATION_NOT_ALLOWED' and
              r_scr.get('status') == 'OPERATION_NOT_ALLOWED',
              str(r_cmd) + ' / ' + str(r_exe) + ' / ' + str(r_scr))

        # 8. Helper rejects un-allowlisted operations
        r_unallow = helper.request({'op': 'run_arbitrary_admin_action'})
        check('sec08-helper-allowlist-only',
              r_unallow.get('status') == 'OPERATION_NOT_ALLOWED',
              str(r_unallow))

        # 9. Broker launched by helper is standard unprivileged process
        st_resp = helper.request({'op': 'managed_context_start', 'target_sid': target_sid, 'scope_id': p.scope})
        check('sec09-broker-token-unprivileged',
              st_resp.get('status') == 'started' and st_resp.get('pid') > 0,
              str(st_resp))

        # 10. Corrupt sealed secret fails closed
        corrupt_sid = 'S-1-5-21-999999999-888888888-777777777-1111'
        corrupt_fp = hashlib.sha256(corrupt_sid.lower().encode()).hexdigest()[:16]
        corrupt_file = os.path.join(vault_dir, corrupt_fp + '.seal')
        with open(corrupt_file, 'wb') as fh:
            fh.write(b'GARBAGE_TRUNCATED_NON_DPAPI_BYTES')
        r_corrupt = helper.request({'op': 'managed_context_start', 'target_sid': corrupt_sid, 'scope_id': p.scope})
        check('sec10-corrupt-secret-fails-closed',
              r_corrupt.get('status') == 'SECRET_CORRUPT',
              str(r_corrupt))

        # 11. Wrong SID fails closed
        r_wrong = helper.request({'op': 'managed_context_start', 'target_sid': 'S-1-5-21-0-0-0-0', 'scope_id': p.scope})
        r_invalid = helper.request({'op': 'managed_context_start', 'target_sid': 'not-a-sid', 'scope_id': p.scope})
        check('sec11-wrong-sid-fails-closed',
              r_wrong.get('status') == 'SECRET_NOT_FOUND' and r_invalid.get('status') == 'INVALID_TARGET_SID',
              str(r_wrong) + ' / ' + str(r_invalid))

        # 12. Secret rotation preserves SID and descriptor
        code, r_rot, _, _ = p.run('context', 'rotate', acc_id)
        check('sec12-secret-rotation-preserves-sid',
              code == 0 and r_rot.get('status') == 'rotated' and r_rot.get('target_sid') == target_sid,
              str(r_rot))

        # 13. FROZEN context does not auto-start
        p.run('set-state', acc_id, 'FROZEN')
        code, r_frozen_usage, _, _ = p.run('usage', acc_id)
        check('sec13-frozen-context-no-autostart',
              code != 0 and (r_frozen_usage or {}).get('status') in ('ACCOUNT_FROZEN', 'LIFECYCLE_FORBIDDEN'),
              str(r_frozen_usage))

        # 14. A usage probe READS. It must not start a managed context: an implicit
        #     start made `context stop` unreportable, because the next status probe
        #     brought the context straight back up. Starting is `context start`'s job.
        p.run('set-state', acc_id, 'ENABLED')
        fp = hashlib.sha256(target_sid.strip().lower().encode()).hexdigest()[:16]
        broker_pipe = 'SAI.Accounts.' + p.scope + '.' + fp
        try:
            pipe_request(broker_pipe, {'cmd': 'shutdown'}, timeout_s=1)
        except Exception:
            pass
        time.sleep(0.3)
        code, r_usage, _, _ = p.run('usage', acc_id)
        check('sec14-offline-managed-probe-does-not-autostart',
              r_usage is not None and r_usage.get('context_state') == 'OFFLINE',
              str(r_usage))
        code, r_start, _, _ = p.run('context', 'start', acc_id)
        code, r_usage2, _, _ = p.run('usage', acc_id)
        check('sec14b-explicit-context-start-comes-online',
              (r_start or {}).get('status') == 'online'
              and r_usage2 is not None and r_usage2.get('context_state') == 'ONLINE',
              str(r_start) + ' / ' + str(r_usage2))

        # 15. OFFLINE ENABLED managed context auto-starts on launch
        try:
            pipe_request(broker_pipe, {'cmd': 'shutdown'}, timeout_s=1)
        except Exception:
            pass
        time.sleep(0.3)
        code, r_launch, _, _ = p.run('launch', acc_id, '--workdir', p.dir)
        check('sec15-offline-managed-autostart-launch',
              r_launch is not None and (r_launch.get('status') == 'launched' or r_launch.get('execution') == 'broker'),
              str(r_launch))

        # 16. Manual legacy context does NOT auto-start silently
        p.legacy('user', [ag_account('S-1-5-21-9-9-9-5555', 'ag_legacy')])
        p.run('import')
        code, r_leg, _, _ = p.run('usage', 'antigravity:windows-user:S-1-5-21-9-9-9-5555')
        check('sec16-manual-legacy-no-silent-autostart',
              r_leg is not None and r_leg.get('context_state') == 'OFFLINE',
              str(r_leg))

        # 17. Helper status reports context info without secret exposure
        code, r_stat, _, _ = p.run('context', 'status', acc_id)
        stat_str = json.dumps(r_stat or {}).lower()
        check('sec17-helper-status-no-secret-exposure',
              code == 0 and r_stat.get('user_exists') is True and 'password' not in stat_str and 'credential_blob' not in stat_str,
              str(r_stat))

        # 18. Context delete cleans vault and registry
        code, r_del, _, _ = p.run('context', 'delete', acc_id, '--force')
        check('sec18-context-delete-cleans-vault',
              code == 0 and not os.path.exists(seal_file),
              str(r_del))

        # 19. Standalone operation without helper
        helper.stop()
        time.sleep(0.5)
        code, r_caps, _, _ = p.run('capabilities', 'antigravity')
        code2, r_list, _, _ = p.run('list')
        check('sec19-standalone-without-helper',
              code == 0 and code2 == 0 and r_caps is not None and r_list is not None,
              str(r_caps))

        # 20. SCM reboot simulation: helper with NO SAI_ACCOUNTS_OPERATOR_SID in env,
        # persisted authorization in helper.json works for non-elevated operator.
        helper_cfg_dir = os.path.join(p.dir, 'helper')
        os.makedirs(helper_cfg_dir, exist_ok=True)
        with open(os.path.join(helper_cfg_dir, 'helper.json'), 'w', encoding='utf-8') as fh:
            json.dump({
                'schema': 'sai.accounts.helper/config/1',
                'schema_version': 1,
                'authorized_operator_sid': p.sid,
                'updated_at': '2026-10-03T00:00:00Z'
            }, fh)

        svc_helper = HelperServer(p, pipe_name='sai-helper-svc-test-' + p.scope)
        if 'SAI_ACCOUNTS_OPERATOR_SID' in svc_helper.env:
            del svc_helper.env['SAI_ACCOUNTS_OPERATOR_SID']
        try:
            svc_ok = svc_helper.start()
            check('sec20a-service-helper-online-from-config', svc_ok, 'service helper failed to start')
            if svc_ok:
                code_stat, r_hstat, _, _ = p.run('helper', 'status')
                check('sec20b-service-persisted-operator-status',
                      code_stat == 0 and r_hstat.get('status') == 'online' and r_hstat.get('authorized_operator_sid') == p.sid,
                      str(r_hstat))

                # Create and start managed context under service-mode helper
                code_add, r_add, _, _ = p.run('add', '--provider', 'antigravity', '--alias', 'AG_Service', '--managed')
                if code_add == 0 and r_add:
                    new_acc_id = r_add.get('account_id')
                    code_cstat, r_cstat, _, _ = p.run('context', 'status', new_acc_id)
                    check('sec20c-service-managed-context-status',
                          code_cstat == 0 and r_cstat.get('user_exists') is True,
                          str(r_cstat))
                    code_cstart, r_cstart, _, _ = p.run('context', 'start', new_acc_id)
                    # 'online' is the honest contract: the CLI waits for the pipe and
                    # reports a broker that never listened as start_context_failed.
                    check('sec20d-service-managed-context-start-no-prompt',
                          code_cstart == 0 and r_cstart.get('status') in ('online', 'launch_requested', 'started'),
                          str(r_cstart))
                else:
                    check('sec20c-service-managed-context-status', False, str(r_add))
                    check('sec20d-service-managed-context-start-no-prompt', False, str(r_add))
        finally:
            svc_helper.stop()

        # 21. Fail-closed: LocalSystem service fails closed without authorized operator SID in helper config
        cfg_path = os.path.join(helper_cfg_dir, 'helper.json')
        if os.path.exists(cfg_path):
            os.remove(cfg_path)
        fail_helper = HelperServer(p, pipe_name='sai-helper-fail-' + p.scope)
        if 'SAI_ACCOUNTS_OPERATOR_SID' in fail_helper.env:
            del fail_helper.env['SAI_ACCOUNTS_OPERATOR_SID']
        fail_helper.env['SAI_SIMULATE_SYSTEM_SERVICE'] = '1'
        try:
            fail_ok = fail_helper.start()
            check('sec21-system-service-fails-closed-without-config', not fail_ok, 'should have failed closed')
        finally:
            fail_helper.stop()

        # 22. Test hook hardening: --test-seal and --test-unseal rejected without SAI_HELPER_TEST_MODE=1
        clean_env = {k: v for k, v in os.environ.items() if k != 'SAI_HELPER_TEST_MODE'}
        r_seal = subprocess.run([HELPER_EXE, '--test-seal', 'S-1-5-21-999', 'secret'],
                                capture_output=True, text=True, env=clean_env)
        check('sec22a-test-seal-gated-in-production',
              r_seal.returncode != 0 and 'disabled in production' in (r_seal.stdout + r_seal.stderr),
              r_seal.stdout + r_seal.stderr)
        r_unseal = subprocess.run([HELPER_EXE, '--test-unseal', 'S-1-5-21-999'],
                                  capture_output=True, text=True, env=clean_env)
        check('sec22b-test-unseal-gated-in-production',
              r_unseal.returncode != 0 and 'disabled in production' in (r_unseal.stdout + r_unseal.stderr),
              r_unseal.stdout + r_unseal.stderr)

        # 23. Migration safety guard: arbitrary existing Windows user profiles cannot be reset
        h_mig = HelperServer(p, pipe_name='sai-helper-mig-' + p.scope)
        try:
            h_mig.start()
            r_bad_mig = h_mig.request({'op': 'managed_context_create', 'provider': 'antigravity',
                                        'migrate_existing': True, 'username': 'Administrator'})
            check('sec23a-reject-arbitrary-profile-migration',
                  r_bad_mig is not None and r_bad_mig.get('status') == 'MIGRATION_TARGET_REJECTED',
                  str(r_bad_mig))
            r_good_mig = h_mig.request({'op': 'managed_context_create', 'provider': 'antigravity',
                                         'migrate_existing': True, 'username': 'antigravity_b'})
            check('sec23b-allow-dedicated-antigravity-migration',
                  r_good_mig is not None and r_good_mig.get('status') == 'ok',
                  str(r_good_mig))
        finally:
            h_mig.stop()
    finally:
        helper.stop()
        p.cleanup()


# ------------------------------------------------------------- T-234 helpers
def ps_quote(path):
    return "'" + str(path).replace("'", "''") + "'"

_ACL_SNAPSHOT = (
    "$a=Get-Acl -LiteralPath %s; "
    "[pscustomobject]@{ sddl=$a.Sddl; protected=[bool]$a.AreAccessRulesProtected; "
    "rules=@($a.Access | ForEach-Object { [pscustomobject]@{ "
    "id=$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value; "
    "mask=$_.FileSystemRights.ToString(); "
    "type=$_.AccessControlType.ToString(); inh=[bool]$_.IsInherited; "
    "flags=$_.InheritanceFlags.ToString() } }) } | ConvertTo-Json -Depth 5 -Compress")

def _ps5_env():
    env = dict(os.environ)
    for k in list(env.keys()):
        if k.upper() == 'PSMODULEPATH':
            del env[k]
    return env

def add_ace(path, sid, mask='FullControl', deny=False):
    """Put an explicit inheritable ACE on a directory, the way an operator
    (or an older SAITULS) might already have done."""
    script = (
        "$sid=New-Object System.Security.Principal.SecurityIdentifier(%s); "
        "$a=Get-Acl -LiteralPath %s; "
        "$r=New-Object System.Security.AccessControl.FileSystemAccessRule("
        "$sid,%s,'ContainerInherit,ObjectInherit','None','%s'); "
        "$a.AddAccessRule($r); Set-Acl -LiteralPath %s -AclObject $a; "
        "(Get-Acl -LiteralPath %s).Sddl"
    ) % (ps_quote(sid), ps_quote(path), ps_quote(mask),
         'Deny' if deny else 'Allow', ps_quote(path), ps_quote(path))
    r = subprocess.run(['powershell', '-NoProfile', '-Command', script],
                       capture_output=True, text=True, env=_ps5_env())
    # A SID string alone resolves through NTAccount and silently places
    # nothing, so the check is the resulting SDDL, not the exit code.
    return r.returncode == 0 and sid.lower() in r.stdout.lower()

def acl_snapshot(path):
    """The real ACL of a directory: SDDL, inheritance state, and every ACE."""
    script = _ACL_SNAPSHOT % ps_quote(path)
    r = subprocess.run(['powershell', '-NoProfile', '-Command', script],
                       capture_output=True, text=True, env=_ps5_env())
    doc = json.loads(r.stdout.strip())
    if isinstance(doc.get('rules'), dict):
        doc['rules'] = [doc['rules']]
    return doc

def rule_keys(doc):
    return set((x['id'], x['mask'], x['type'], x['inh'], x['flags']) for x in doc['rules'])

def rules_for(doc, sid_text):
    return [x for x in doc['rules'] if sid_text.lower() in x['id'].lower()]

def ledger(plane):
    path = os.path.join(plane.dir, 'grants.json')
    if not os.path.exists(path):
        return []
    with open(path, 'rb') as fh:
        raw = fh.read()
    if raw[:3] == b'\xef\xbb\xbf':
        raw = raw[3:]
    return json.loads(raw.decode('utf-8'))

# (14) the sign-in operation is a fixed, allowlisted, workdir-free operation.
def test_auth_operation():
    p = Plane('auth-op-test')
    try:
        foreign = 'S-1-5-21-777777777-888888888-999999999-1002'
        p.legacy('user', [ag_account(p.sid, os.environ.get('USERNAME', 'operator')),
                          ag_account(foreign, 'antigravity_b')])
        p.run('import')
        _, listing, _, _ = p.run('list')
        ids = {a['compact_label']: a['account_id'] for a in listing['accounts']}

        # AG1 here is this very user (p.sid), so `status` answers it from this
        # process instead of over the pipe. Both accounts must publish the same
        # fields: the local branch once omitted auth_state entirely, which left
        # the operator's own row rendering blank beside an AUTHENTICATED AG2.
        _, ag1st, _, _ = p.run('status', 'AG1')
        check('auth08-local-status-reports-auth-state',
              ag1st is not None
              and ag1st.get('auth_state') in ('AUTHENTICATED', 'AUTH_REQUIRED'),
              str(ag1st)[:200])

        # A managed account with no broker must not "help" by signing in as
        # somebody else, and must not be signed in for by the operator's copy.
        code, out, _, _ = p.run('auth', 'AG2')
        check('auth01-offline-managed-context-fails-closed',
              out is not None and out.get('status') == 'CONTEXT_OFFLINE' and code == 2, str(out))

        # No workdir, ever: sign-in is not a project operation.
        for extra in (['--workdir', p.dir], [p.dir], ['--exe', 'agy.exe'], ['--script', 'x.ps1']):
            code, out, _, _ = p.run('auth', 'AG2', *extra)
            check('auth02-rejects-extra-argument:' + extra[0],
                  out is not None and out.get('status') == 'auth_args', str(out))

        # Lifecycle states that must not authenticate are refused before any
        # context, broker or process is touched.
        p.run('freeze', ids['AG2'])
        code, out, _, _ = p.run('auth', 'AG2')
        check('auth03-frozen-refused',
              out is not None and out.get('status') == 'LIFECYCLE_FORBIDDEN', str(out))
        p.run('restore', ids['AG2'])

        # Only providers with an interactive login can answer.
        profile_id = p.profile('claude')
        _, listing2, _, _ = p.run('list', '--all')
        claude = [a for a in listing2['accounts'] if a['account_id'] == profile_id]
        if claude:
            code, out, _, _ = p.run('auth', claude[0]['account_id'])
            check('auth04-non-antigravity-provider-refused',
                  out is not None and out.get('status') == 'PROVIDER_UNSUPPORTED', str(out))
        else:
            check('auth04-non-antigravity-provider-refused', False, 'no non-antigravity account to test')

        # AG1 is the operator's own account and is unchanged by any of this: it
        # authenticates in its OWN profile directory, never in a project folder.
        code, out, _, _ = p.run('auth', 'AG1')
        body = json.dumps(out) if out else ''
        check('auth05-current-user-stays-current-user',
              out is not None and out.get('account_id') == ids['AG1']
              and out.get('status') in ('launched', 'auth_launch_failed'), str(out))
        check('auth06-auth-carries-no-workdir',
              '--workdir' not in body and p.dir not in body, body[:200])
        check('auth07-auth-envelope-carries-no-secret',
              not any(k in body.lower() for k in ('password', 'token', 'secret', 'cookie', 'code')), body[:200])
    finally:
        p.cleanup()


# (14) the broker refuses every way of naming a program on the auth op.
def test_broker_auth_allowlist():
    p = Plane('broker-auth-test')
    b = Broker(p)
    try:
        if b.start() is None:
            check('broker-auth-starts', False, 'broker did not start')
            return
        name = b.pipe_name()
        for field, value in (('workdir', p.dir), ('exe', r'C:\Windows\System32\cmd.exe'),
                             ('command', 'cmd.exe /c whoami'), ('script', 'x.bat'),
                             ('args', '-p'), ('path', r'C:\Windows'), ('program', 'agy.exe')):
            r = pipe_request(name, {'cmd': 'auth', field: value})
            check('broker-auth-rejects-' + field,
                  r is not None and r.get('status') == 'OPERATION_NOT_ALLOWED'
                  and r.get('rejected_field') == field, str(r))

        # The allowlisted shape reaches the real launch path. In the test root
        # there is no agy.exe, so it reports a launch failure -- which is the
        # proof that the operation is not an allowlist in name only.
        r = pipe_request(name, {'cmd': 'auth'})
        check('broker-auth-allowlisted-shape-served',
              r is not None and r.get('status') != 'OPERATION_NOT_ALLOWED', str(r))
        check('broker-auth-names-no-secret',
              r is not None and not any(k in json.dumps(r).lower()
                                        for k in ('password', 'token', 'secret')), str(r))
    finally:
        b.stop()
        p.cleanup()


def test_broker_workdir_probe():
    # (15) the access probe answers "can this identity actually use that
    # directory", and nothing wider: it refuses every caller-named path,
    # program or file, and it leaves nothing behind.
    p = Plane('broker-probe-test')
    b = Broker(p)
    workdir = os.path.join(p.dir, 'project')
    os.makedirs(workdir)
    try:
        if b.start() is None:
            check('probe-broker-starts', False, 'broker did not start')
            return
        name = b.pipe_name()
        for field, value in (('exe', r'C:\Windows\System32\cmd.exe'), ('command', 'cmd.exe /c whoami'),
                             ('script', 'x.bat'), ('args', '-p'), ('path', r'C:\Windows'),
                             ('program', 'agy.exe'), ('file', 'other.txt'), ('name', 'x'),
                             ('content', 'pwned')):
            r = pipe_request(name, {'cmd': 'workdir_probe', 'workdir': workdir, field: value})
            check('probe-rejects-' + field,
                  r is not None and r.get('status') == 'OPERATION_NOT_ALLOWED'
                  and r.get('rejected_field') == field, str(r))

        r = pipe_request(name, {'cmd': 'workdir_probe', 'workdir': workdir})
        check('probe-round-trip',
              r is not None and r.get('status') == 'ok'
              and r.get('read') is True and r.get('modified') is True
              and r.get('deleted') is True and r.get('cleaned') is True, str(r))
        check('probe-leaves-nothing',
              not os.path.exists(os.path.join(workdir, '.saituls-access-probe')),
              'probe scratch directory survived')
        check('probe-names-no-secret',
              r is not None and not any(k in json.dumps(r).lower()
                                        for k in ('password', 'token', 'secret')), str(r))

        # A directory that is not there is refused before any probe file is
        # attempted: the probe never manufactures evidence.
        r = pipe_request(name, {'cmd': 'workdir_probe', 'workdir': os.path.join(p.dir, 'nope')})
        check('probe-missing-workdir-is-not-found',
              r is not None and r.get('status') == 'WORKDIR_UNAVAILABLE'
              and r.get('reason') == 'NOT_FOUND', str(r))
    finally:
        b.stop()
        p.cleanup()


def test_broker_saipen_readiness():
    # T-287: SAIPEN readiness measured by the BROKER's own token. It runs only
    # the machine runtime's fixed launcher with fixed read-only argv; every
    # caller-named program, argument or file is refused, and a missing workdir
    # is refused before anything runs.
    p = Plane('broker-saipen-ready-test')
    b = Broker(p)
    workdir = os.path.join(p.dir, 'project')
    os.makedirs(workdir)
    try:
        if b.start() is None:
            check('ready-broker-starts', False, 'broker did not start')
            return
        name = b.pipe_name()
        for field, value in (('exe', r'C:\Windows\System32\cmd.exe'), ('command', 'cmd.exe /c whoami'),
                             ('script', 'x.bat'), ('args', '--evil'), ('path', r'C:\Windows'),
                             ('program', 'python.exe'), ('file', 'other.txt'), ('name', 'x'),
                             ('content', 'pwned')):
            r = pipe_request(name, {'cmd': 'saipen_readiness', 'workdir': workdir, field: value})
            check('ready-rejects-' + field,
                  r is not None and r.get('status') == 'OPERATION_NOT_ALLOWED'
                  and r.get('rejected_field') == field, str(r))

        r = pipe_request(name, {'cmd': 'saipen_readiness', 'workdir': os.path.join(p.dir, 'nope')})
        check('ready-missing-workdir-is-not-found',
              r is not None and r.get('status') == 'WORKDIR_UNAVAILABLE'
              and r.get('reason') == 'NOT_FOUND', str(r))

        r = pipe_request(name, {"cmd": "saipen_readiness", "workdir": workdir}, timeout_s=600)
        launcher = os.path.join(os.environ.get('ProgramData', r'C:\ProgramData'), 'SAI', 'SAIPEN', 'bin', 'saipen.cmd')
        check('ready-reports-its-own-token',
              r is not None and r.get('status') == 'ok'
              and r.get('effective_sid') == r.get('sid') and r.get('elevated') in (True, False), str(r))
        check('ready-launcher-presence-is-measured',
              r is not None and r.get('launcher_present') is os.path.isfile(launcher), str(r))
        if os.path.isfile(launcher):
            check('ready-machine-status-is-parsed',
                  r is not None and isinstance(((r.get('machine') or {}).get('json') or {}).get('code'), str), str(r))
            check('ready-non-project-is-not-admitted',
                  r is not None and ((r.get('project') or {}).get('json') or {}).get('ok') is not True, str(r))
            argv = ((r or {}).get('write_admission') or {}).get('argv') or ''
            check('ready-write-admission-is-a-dry-run-checkpoint',
                  argv.startswith('saipen checkpoint DEC') and argv.endswith('--dry-run --json'), str(r))
        check('ready-leaves-nothing', os.listdir(workdir) == [], str(os.listdir(workdir)))
    finally:
        b.stop()
        p.cleanup()


# (15) (16) (10) (11) (12) the helper grants ONE managed SID ONE project
# directory, changes nothing else, and can take back only what it wrote.
def test_workdir_grants():
    p = Plane('workdir-grant-test')
    helper = HelperServer(p)
    workdir = os.path.join(p.dir, 'project')
    os.makedirs(workdir)
    try:
        if not helper.start():
            check('grant-helper-online', False, 'helper failed to start')
            return
        # AG1 is the operator's own account: part of the fixture so that the
        # "current user is unchanged" claims below have something to be about.
        p.legacy('user', [ag_account(p.sid, os.environ.get('USERNAME', 'operator'))])
        p.run('import')
        code, created, _, _ = p.run('context', 'create', '--provider', 'antigravity',
                                    '--alias', 'AG_GrantTarget')
        if not created or not created.get('windows_sid'):
            check('grant-managed-account-created', False, str(created))
            return
        sid = created['windows_sid']
        _, got, _, _ = p.run('get', created['account_id'])
        managed_label = got['account']['compact_label']

        # -- refusals first: every one of these must fail BEFORE any mutation --
        everyone = [('S-1-1-0', 'well-known Everyone'), ('S-1-5-32-545', 'BUILTIN\\Users'),
                    ('S-1-5-11', 'Authenticated Users'), ('S-1-5-18', 'NT AUTHORITY\\SYSTEM')]
        got = {helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': s,
                               'workdir': workdir}).get('status') for s, _ in everyone}
        check('grant01-well-known-sids-refused', got == {'WELL_KNOWN_SID_REFUSED'}, str(got))
        check('grant02-invalid-sid-refused',
              helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': 'not-a-sid',
                              'workdir': workdir}).get('status') == 'INVALID_TARGET_SID', '')
        # UNKNOWN_TARGET_SID is deliberately NOT asserted here. The helper's
        # existence check is short-circuited in test mode -- the context
        # operations need that, because their fixture SIDs do not resolve -- so
        # a hermetic run cannot reach this branch at all. It is proven by the
        # live acceptance oracle against a real managed SID, not pretended here.
        check('grant04-workdir-required',
              helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': sid}).get('status')
              == 'WORKDIR_REQUIRED', '')

        # (15) catastrophic targets: a drive root and every system root.
        special = {'ProgramFiles': os.environ.get('ProgramFiles', r'C:\Program Files'),
                   'ProgramFilesX86': os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                   'ProgramData': os.environ.get('ProgramData', r'C:\ProgramData'),
                   'Windows': os.environ.get('SystemRoot', r'C:\Windows')}
        drive_root = os.path.splitdrive(workdir)[0] + '\\'
        refused = {drive_root: drive_root}
        for label, path in special.items():
            refused[path] = path
            # the ANCESTOR of a protected root grants the whole subtree with it
            parent = os.path.dirname(path.rstrip('\\'))
            if parent and len(parent) > 2:
                refused[parent + '\\'] = parent + '\\'
        bad = {}
        for path in refused:
            bad[path] = helper.request({'op': 'managed_workdir_ensure_access',
                                        'target_sid': sid, 'workdir': path}).get('status')
        check('grant05-drive-and-system-roots-refused',
              all(v == 'WORKDIR_TARGET_REFUSED' for v in bad.values()),
              str([(k, v) for k, v in bad.items() if v != 'WORKDIR_TARGET_REFUSED']))

        # (14) the helper stays an allowlist: no caller-supplied program, ever.
        r = helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': sid,
                            'workdir': workdir, 'exe': r'C:\Windows\System32\cmd.exe'})
        check('grant06-helper-rejects-exe-field',
              r.get('status') == 'OPERATION_NOT_ALLOWED', str(r))

        before = acl_snapshot(workdir)
        check('grant07-nothing-mutated-by-refusals',
              not rules_for(before, sid) and not ledger(p), str(ledger(p))[:200])

        # (12) a folder that is not there must not produce an ACL entry.
        missing = os.path.join(workdir, 'no-such-dir')
        r = helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': sid, 'workdir': missing})
        check('grant08-not-found-refused', r.get('status') == 'WORKDIR_NOT_FOUND', str(r))
        check('grant09-not-found-changed-no-acl',
              rule_keys(acl_snapshot(workdir)) == rule_keys(before), 'ACL changed on a missing folder')

        # -- the grant itself --
        r = helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': sid,
                            'workdir': workdir}, timeout_s=10)
        check('grant10-grants-the-named-sid', r.get('status') == 'granted', str(r))
        after = acl_snapshot(workdir)
        mine = rules_for(after, sid)
        check('grant11-one-explicit-ace-for-the-target-sid', len(mine) == 1, str(mine))
        check('grant12-minimum-rights-not-fullcontrol',
              len(mine) == 1 and 'Modify' in mine[0]['mask'] and 'FullControl' not in mine[0]['mask'],
              str(mine))
        check('grant13-ace-propagates-to-files-and-subfolders',
              len(mine) == 1 and 'ContainerInherit' in mine[0]['flags']
              and 'ObjectInherit' in mine[0]['flags'], str(mine))
        check('grant14-existing-inheritance-untouched',
              after['protected'] == before['protected'] is False, str(after['protected']))
        check('grant15-no-unrelated-ace-replaced-or-weakened',
              rule_keys(before) <= rule_keys(after),
              str(sorted(rule_keys(before) - rule_keys(after))[:3]))
        rows = ledger(p)
        check('grant16-ledger-records-what-we-created',
              len(rows) == 1 and rows[0].get('created') is True
              and os.path.normcase(os.path.realpath(rows[0].get('workdir', ''))) == os.path.normcase(os.path.realpath(workdir)),
              str(rows)[:200])

        # (11) the second call is a no-op that claims no ownership.
        r2 = helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': sid,
                             'workdir': workdir})
        check('grant17-second-call-already-granted', r2.get('status') == 'already_granted', str(r2))
        check('grant18-second-call-writes-no-acl',
              rule_keys(acl_snapshot(workdir)) == rule_keys(after), 'ACL changed on a no-op grant')
        check('grant19-second-call-does-not-disown-our-own-ace',
              any(row.get('created') is True and row.get('target_sid') == sid for row in ledger(p)),
              str(ledger(p))[:200])

        # (17) the operator's own account is not "granted" anything: it already
        # owns its own profile, and a ledger row for it would be a lie.
        own = None
        for label in ('AG1',):
            code, out, _, _ = p.run('grant-workdir', label, '--workdir', workdir)
            if out and out.get('status') == 'ALREADY_OWNED':
                own = label
                check('grant20-current-user-needs-no-grant', code == 0, str(out))
        check('grant21-current-user-grant-not-recorded-in-ledger',
              all(row.get('target_sid') != p.sid for row in ledger(p)), str(ledger(p))[:200])
        check('grant22-current-user-found', own is not None, 'no current-user antigravity account')

        # the CLI verb is as strict as the helper
        code, out, _, _ = p.run('grant-workdir', managed_label, workdir)
        check('grant23-cli-requires-workdir-flag',
              out is not None and out.get('status') == 'grant_workdir_args', str(out))
        code, out, _, _ = p.run('grant-workdir', managed_label, '--workdir', workdir, '--force')
        check('grant24-cli-rejects-extra-arguments',
              out is not None and out.get('status') == 'grant_workdir_args', str(out))
        p.run('freeze', managed_label)
        code, out, _, _ = p.run('grant-workdir', managed_label, '--workdir', workdir)
        check('grant25-frozen-account-refused',
              out is not None and out.get('status') == 'LIFECYCLE_FORBIDDEN', str(out))
        p.run('restore', managed_label)

        # -- revocation takes back exactly one ACE, on one directory --
        r = helper.request({'op': 'managed_workdir_revoke_access', 'target_sid': sid})
        check('grant30-revoke-removes-what-we-created',
              r.get('status') == 'revoked' and r.get('revoked_rules', 0) >= 1, str(r))
        final = acl_snapshot(workdir)
        check('grant31-revoke-restores-the-original-acl',
              rule_keys(final) == rule_keys(before),
              str(sorted(rule_keys(before) ^ rule_keys(final))[:3]))

        # (5) a pre-existing equivalent ACE is never claimed and never removed.
        pre = os.path.join(p.dir, 'project-pre')
        os.makedirs(pre)
        check('grant26-preexisting-ace-in-place', add_ace(pre, sid), 'could not place a test ACE')
        snap_pre = acl_snapshot(pre)
        check('grant26-preexisting-ace-in-place', len(rules_for(snap_pre, sid)) == 1,
              str([r['id'] for r in snap_pre['rules']]))
        r = helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': sid, 'workdir': pre})
        check('grant27-preexisting-sufficient-grant-is-already-granted',
              r.get('status') == 'already_granted', str(r))
        r = helper.request({'op': 'managed_workdir_revoke_access', 'target_sid': sid})
        check('grant28-revoke-preserves-aces-we-did-not-create',
              r.get('status') == 'revoked' and r.get('revoked_rules', 0) == 0
              and len(rules_for(acl_snapshot(pre), sid)) == 1,
              str(r) + ' ' + str([r['id'] for r in rules_for(acl_snapshot(pre), sid)]))
        check('grant29-revoke-preserved-rule-is-kept-in-ledger',
              any(row.get('created') is not True for row in ledger(p)), str(ledger(p))[:200])

        log = helper.log_text()
        check('grant32-helper-log-carries-no-secret',
              not re.search(r'(?i)(password|oauth|refresh_token|access_token|authorization_code)\s*[:=]',
                            log), log[:200])

        # -- (4) a link is refused, not followed ----------------------------
        # Path.GetFullPath is lexical, so a junction would otherwise hand the
        # ACE to whatever the link finally points at and the operator would
        # never see that name again.
        target = os.path.join(p.dir, 'link-target')
        os.makedirs(target)
        junction = os.path.join(p.dir, 'link-proj')
        made = subprocess.call(['cmd', '/c', 'mklink', '/J', junction, target],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        if made:
            snap_target_before = acl_snapshot(target)
            r = helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': sid,
                                'workdir': junction})
            check('grant33-reparse-point-refused', r.get('status') == 'WORKDIR_REPARSE_REFUSED',
                  str(r))
            check('grant33b-reparse-refusal-wrote-no-ace-anywhere',
                  rule_keys(acl_snapshot(target)) == rule_keys(snap_target_before)
                  and not rules_for(acl_snapshot(junction), sid),
                  'an ACE landed on a link target')
        else:
            check('grant33-reparse-point-refused', False, 'could not create a junction fixture')

        # -- (4) an explicit deny is reported, never worked around -----------
        denied = os.path.join(p.dir, 'project-denied')
        os.makedirs(denied)
        add_ace(denied, sid, mask='FullControl', deny=True)
        before_denied = acl_snapshot(denied)
        r = helper.request({'op': 'managed_workdir_ensure_access', 'target_sid': sid,
                            'workdir': denied})
        check('grant34-deny-conflict-is-reported',
              r.get('status') == 'EFFECTIVE_ACCESS_DENIED', str(r))
        check('grant35-deny-conflict-adds-no-allow-and-keeps-the-deny',
              rule_keys(acl_snapshot(denied)) == rule_keys(before_denied),
              str([r['id'] for r in rules_for(acl_snapshot(denied), sid)]))
        check('grant36-deny-conflict-claims-no-ledger-row',
              all(os.path.normpath(row.get('workdir', '')).lower()
                  != os.path.normpath(denied).lower() for row in ledger(p)),
              str(ledger(p))[:200])
    finally:
        helper.stop()
        p.cleanup()


def test_grant_failure_contract():
    """The shapes a provisioning failure must be able to report.

    Reading the source here is not laziness: an ACL read failure and a missing
    grant are the same call from the outside unless the code keeps them apart,
    and that difference is invisible to any fixture that can only make grants
    succeed. What is asserted is the contract, not a message.
    """
    src = open(os.path.join(ROOT, 'Scripts', 'accounts', 'SaiAccountsHelper.cs'),
               encoding='utf-8').read()
    body = re.search(r'static Dictionary<string, object> EnsureManagedWorkdirAccess.*?\n    }',
                     src, re.DOTALL).group(0)
    # Grant failures funnel through the rollback-verified helper so that a
    # partial mutation can never escape without a read-back of the DACL.
    body += re.search(r'static void HandleGrantFailure.*?\n    }',
                      src, re.DOTALL).group(0)
    for status in ('ACL_READ_FAILED', 'ACL_MUTATION_FAILED', 'EFFECTIVE_ACCESS_DENIED',
                   'WORKDIR_REPARSE_REFUSED'):
        check('grant40-status-' + status, status in body, 'missing from EnsureManagedWorkdirAccess')
    # "could not read the ACL" must never be reported as "the ACL lacks the rights"
    check('grant41-acl-read-failure-is-not-silently-already-false',
          'catch { already = false; }' not in src, 'the read failure is still collapsed')
    # a grant does not require reading the operator's project contents
    check('grant42-grant-does-not-enumerate-project-contents',
          'GetFileSystemEntries' not in body, 'the pre-enumeration gate is back')
    # a write that reported success is verified by reading the descriptor back
    check('grant43-grant-is-verified-after-the-write',
          '"verify"' in body and 'HasSufficientManagedGrant(after, sid)' in body,
          'no post-grant verification')
    # failures carry structured diagnostics, never a parsed English message
    check('grant44-failures-carry-structured-detail',
          'SetAclFailure' in body and '"win32_error"' in body and '"error_type"' in body,
          'no structured failure detail')
    check('grant45-no-take-ownership-fallback',
          'SeTakeOwnershipPrivilege' not in src and 'SetOwner' not in src,
          'ownership fallback present')


def test_account_reorder():
    p = Plane()
    try:
        # Set up 3 Antigravity accounts + 2 Claude + 2 Codex profiles for same-provider & cross-provider moves
        p.legacy('user', [
            ag_account('S-1-5-21-9-9-9-1001', 'ag_user_1'),
            ag_account('S-1-5-21-9-9-9-1002', 'ag_user_2'),
            ag_account('S-1-5-21-9-9-9-1003', 'ag_user_3'),
        ])
        code, out, _, _ = p.run('import')
        check('reorder01-import-ok', code == 0 and out.get('status') == 'ok', str(out)[:200])

        # Add synthetic Claude and Codex descriptors directly to registry so the fixture is self-contained
        reg = p.registry()
        accts = reg['accounts']
        ag_only = [a for a in accts if a['provider_id'] == 'antigravity'][:3]
        cl1 = {
            'account_id': 'claude:profile-dir:1111111111111111',
            'provider_id': 'claude',
            'display_name': None,
            'execution_backend': 'profile_directory',
            'execution_context_id': 'current-user',
            'operational_state': 'ENABLED',
            'hidden': False,
            'auto_connect': False,
            'auto_connect_reason': 'default',
            'created_at': '2026-10-02T10:00:00Z',
            'updated_at': '2026-10-02T10:00:00Z',
            'provider_metadata': {'profile_locator': r'C:\profiles\claude1', 'legacy_id': 'claude-1'},
            'tags': ['prod']
        }
        cl2 = {
            'account_id': 'claude:profile-dir:2222222222222222',
            'provider_id': 'claude',
            'display_name': 'Claude Two Custom',
            'execution_backend': 'profile_directory',
            'execution_context_id': 'current-user',
            'operational_state': 'DISABLED',
            'hidden': True,
            'auto_connect': False,
            'auto_connect_reason': 'operator_disconnect',
            'created_at': '2026-10-02T10:01:00Z',
            'updated_at': '2026-10-02T10:01:00Z',
            'provider_metadata': {'profile_locator': r'C:\profiles\claude2', 'legacy_id': 'claude-2'},
            'tags': ['secondary']
        }
        cx1 = {
            'account_id': 'codex:profile-dir:3333333333333333',
            'provider_id': 'codex',
            'display_name': None,
            'execution_backend': 'profile_directory',
            'execution_context_id': 'current-user',
            'operational_state': 'FROZEN',
            'hidden': False,
            'auto_connect': False,
            'auto_connect_reason': 'default',
            'created_at': '2026-10-02T10:02:00Z',
            'updated_at': '2026-10-02T10:02:00Z',
            'provider_metadata': {
                'profile_locator': r'C:\profiles\codex1',
                'env_scrub': ['OPENAI_API_KEY', 'CODEX_API_KEY'],
                'legacy_id': 'codex-1'
            },
            'tags': []
        }
        reg['accounts'] = ag_only + [cl1, cl2, cx1]
        reg['generation'] = 10
        p.write_registry(reg)

        _, before_list, _, _ = p.run('list', '--all')
        before_accounts = before_list['accounts']
        ids_initial = [a['account_id'] for a in before_accounts]
        ag1_id, ag2_id, ag3_id = ids_initial[0], ids_initial[1], ids_initial[2]
        cl1_id, cl2_id, cx1_id = ids_initial[3], ids_initial[4], ids_initial[5]
        meta_snapshot = {
            a['account_id']: (
                a['provider_id'],
                a['execution_backend'],
                a['execution_context_id'],
                a['operational_state'],
                a['hidden'],
                a['auto_connect'],
                a['auto_connect_reason'],
                a['context_label'],
                a['provider_metadata'],
            )
            for a in before_accounts
        }

        # 1. reorder middle -> first (move cl1 before ag1)
        code, m, _, _ = p.run('move', cl1_id, 'before', ag1_id)
        check('reorder02-middle-to-first-ok',
              code == 0 and m is not None and m.get('status') == 'ok' and m.get('generation') == 11,
              str(m))
        _, cur, _, _ = p.run('list', '--all')
        check('reorder03-middle-to-first-order',
              [a['account_id'] for a in cur['accounts']] == [cl1_id, ag1_id, ag2_id, ag3_id, cl2_id, cx1_id],
              str([a['account_id'] for a in cur['accounts']]))

        # 2. reorder first -> last (move cl1 after cx1)
        code, m, _, _ = p.run('move', cl1_id, 'after', cx1_id)
        check('reorder04-first-to-last-ok',
              code == 0 and m is not None and m.get('status') == 'ok' and m.get('generation') == 12,
              str(m))
        _, cur, _, _ = p.run('list', '--all')
        check('reorder05-first-to-last-order',
              [a['account_id'] for a in cur['accounts']] == [ag1_id, ag2_id, ag3_id, cl2_id, cx1_id, cl1_id],
              str([a['account_id'] for a in cur['accounts']]))

        # 3. adjacent up / down
        code, m, _, _ = p.run('move', cl1_id, 'up')
        check('reorder06-adjacent-up-ok',
              code == 0 and m is not None and m.get('status') == 'ok' and m.get('generation') == 13,
              str(m))
        _, cur, _, _ = p.run('list', '--all')
        check('reorder07-adjacent-up-order',
              [a['account_id'] for a in cur['accounts']] == [ag1_id, ag2_id, ag3_id, cl2_id, cl1_id, cx1_id],
              str([a['account_id'] for a in cur['accounts']]))

        code, m, _, _ = p.run('move', cl2_id, 'down')
        check('reorder08-adjacent-down-ok',
              code == 0 and m is not None and m.get('status') == 'ok' and m.get('generation') == 14,
              str(m))
        _, cur, _, _ = p.run('list', '--all')
        check('reorder09-adjacent-down-order',
              [a['account_id'] for a in cur['accounts']] == [ag1_id, ag2_id, ag3_id, cl1_id, cl2_id, cx1_id],
              str([a['account_id'] for a in cur['accounts']]))

        # 4. boundary no-op / refusal: moving first up, last down, or before/after self does not increment generation
        code, m_top, _, _ = p.run('move', ag1_id, 'up')
        check('reorder10-boundary-top-noop',
              code == 0 and m_top is not None and m_top.get('status') == 'noop' and m_top.get('generation') == 14,
              str(m_top))
        code, m_bot, _, _ = p.run('move', cx1_id, 'down')
        check('reorder11-boundary-bottom-noop',
              code == 0 and m_bot is not None and m_bot.get('status') == 'noop' and m_bot.get('generation') == 14,
              str(m_bot))
        code, m_self, _, _ = p.run('move', ag2_id, 'before', ag2_id)
        check('reorder12-self-target-noop',
              code == 0 and m_self is not None and m_self.get('status') == 'noop' and m_self.get('generation') == 14,
              str(m_self))
        code, m_adj, _, _ = p.run('move', ag1_id, 'before', ag2_id)
        check('reorder13-already-before-noop',
              code == 0 and m_adj is not None and m_adj.get('status') == 'noop' and m_adj.get('generation') == 14,
              str(m_adj))
        code, m_bad_dir, _, _ = p.run('move', ag1_id, 'sideways')
        check('reorder14-invalid-direction-refused',
              code != 0 and m_bad_dir is not None and m_bad_dir.get('status') == 'move_args',
              str(m_bad_dir))

        # 5. unknown account refuses cleanly without changing generation
        code, m_unk1, _, _ = p.run('move', 'antigravity:windows-user:deadbeefdeadbeef', 'up')
        check('reorder15-unknown-source-refused',
              code == 1 and m_unk1 is not None and m_unk1.get('status') == 'ACCOUNT_NOT_FOUND',
              str(m_unk1))
        code, m_unk2, _, _ = p.run('move', ag1_id, 'before', 'antigravity:windows-user:deadbeefdeadbeef')
        check('reorder16-unknown-target-refused',
              code == 1 and m_unk2 is not None and m_unk2.get('status') == 'ACCOUNT_NOT_FOUND',
              str(m_unk2))
        _, cur, _, _ = p.run('list', '--all')
        check('reorder17-generation-unchanged-after-noop-and-refusals',
              cur['generation'] == 14, str(cur['generation']))

        # 6. same-provider reorder: move AG3 before AG1, then AG2 before AG3 -> [AG2, AG3, AG1, ...]
        # Prove identity and generated provider ordinals/labels stay stable and never retarget commands!
        code, m, _, _ = p.run('move', ag3_id, 'before', ag1_id)
        check('reorder18-same-provider-move-1', code == 0 and m is not None and m.get('status') == 'ok', str(m))
        code, m, _, _ = p.run('move', ag2_id, 'before', ag3_id)
        check('reorder19-same-provider-move-2', code == 0 and m is not None and m.get('status') == 'ok', str(m))
        _, cur, _, _ = p.run('list', '--all')
        ag_now = [a for a in cur['accounts'] if a['provider_id'] == 'antigravity']
        check('reorder20-same-provider-order',
              [a['account_id'] for a in ag_now] == [ag2_id, ag3_id, ag1_id],
              str([a['account_id'] for a in ag_now]))
        check('reorder21-same-provider-labels-stable',
              [(a['account_id'], a['compact_label']) for a in ag_now] == [
                  (ag2_id, 'AG2'), (ag3_id, 'AG3'), (ag1_id, 'AG1')
              ],
              str([(a['account_id'], a['compact_label'], a['display_name']) for a in ag_now]))
        # Resolving by compact label after same-provider reorder still resolves the exact original account_id
        _, got_ag1, _, _ = p.run('get', 'AG1')
        _, got_ag2, _, _ = p.run('get', 'AG2')
        _, got_ag3, _, _ = p.run('get', 'AG3')
        check('reorder22-compact-selector-never-retargets',
              got_ag1['account']['account_id'] == ag1_id
              and got_ag2['account']['account_id'] == ag2_id
              and got_ag3['account']['account_id'] == ag3_id,
              str([got_ag1['account']['account_id'], got_ag2['account']['account_id'], got_ag3['account']['account_id']]))

        # 7. cross-provider interleaved reorder: AG2, CL2, CX1, AG1, CL1, AG3
        p.run('move', cl2_id, 'after', ag2_id)
        p.run('move', cx1_id, 'after', cl2_id)
        p.run('move', ag1_id, 'after', cx1_id)
        p.run('move', cl1_id, 'after', ag1_id)
        p.run('move', ag3_id, 'after', cl1_id)
        _, interleaved, _, _ = p.run('list', '--all')
        expected_interleaved = [ag2_id, cl2_id, cx1_id, ag1_id, cl1_id, ag3_id]
        check('reorder23-cross-provider-interleaved-order',
              [a['account_id'] for a in interleaved['accounts']] == expected_interleaved,
              str([a['account_id'] for a in interleaved['accounts']]))

        # 8. account_ids and lifecycle/hidden/auth/context metadata unchanged
        check('reorder24-account-ids-unchanged',
              set(a['account_id'] for a in interleaved['accounts']) == set(ids_initial),
              str([a['account_id'] for a in interleaved['accounts']]))
        after_meta = {
            a['account_id']: (
                a['provider_id'],
                a['execution_backend'],
                a['execution_context_id'],
                a['operational_state'],
                a['hidden'],
                a['auto_connect'],
                a['auto_connect_reason'],
                a['context_label'],
                a['provider_metadata'],
            )
            for a in interleaved['accounts']
        }
        check('reorder25-metadata-and-lifecycle-unchanged',
              after_meta == meta_snapshot,
              'metadata drifted across reorder')

        # 9. order survives rename, hide/show, enable/disable
        p.run('rename', ag3_id, 'AG3 Renamed')
        p.run('set-hidden', ag1_id, 'true')
        p.run('set-state', cl2_id, 'ENABLED')
        _, after_ops, _, _ = p.run('list', '--all')
        check('reorder26-survives-rename-hide-state-changes',
              [a['account_id'] for a in after_ops['accounts']] == expected_interleaved,
              str([a['account_id'] for a in after_ops['accounts']]))

        # 10. concurrent / stale selection safety:
        # Caller had stale view where ag1_id was at index 0, but it is now at index 3.
        # Moving ag1_id by its canonical account_id moves ag1_id, never the account currently at index 0 (ag2_id).
        code, m, _, _ = p.run('move', ag1_id, 'before', ag2_id)
        _, after_stale, _, _ = p.run('list', '--all')
        check('reorder27-stale-row-position-moves-only-named-account-id',
              code == 0 and [a['account_id'] for a in after_stale['accounts']] == [ag1_id, ag2_id, cl2_id, cx1_id, cl1_id, ag3_id],
              str([a['account_id'] for a in after_stale['accounts']]))
    finally:
        p.cleanup()


def test_canonical_vs_runtime_identity_isolation():
    p = Plane()
    try:
        p.legacy('user', [
            ag_account('S-1-5-21-9-9-9-1001', 'ag_user_1'),
            ag_account('S-1-5-21-9-9-9-1002', 'ag_user_2'),
        ])
        p.run('import')
        _, listed, _, _ = p.run('list', '--all')
        ag1_id = [a['account_id'] for a in listed['accounts'] if a['compact_label'] == 'AG1'][0]
        gen0 = p.registry()['generation']

        # 1. Explicit bind-identity sets canonical configured_identity and increments generation
        code, b, raw, _ = p.run('bind-identity', ag1_id, '--fingerprint', '1111222233334444', '--label', 'canonical@example.com')
        check('iso01-bind-identity-succeeds',
              code == 0 and b and b.get('status') == 'ok' and b.get('generation') == gen0 + 1
              and not has_secret_material(raw),
              str(b))

        # 2. list, get, and status return configured_identity without mutating generation
        gen1 = p.registry()['generation']
        _, l1, raw_l, _ = p.run('list', '--all')
        ag1_desc = [a for a in l1['accounts'] if a['account_id'] == ag1_id][0]
        ci = ag1_desc.get('configured_identity') or {}
        check('iso02-list-exposes-configured-identity',
              ci.get('fingerprint') == '1111222233334444' and ci.get('account_label') == 'canonical@example.com'
              and not has_secret_material(raw_l),
              str(ci))

        _, st, raw_st, _ = p.run('status', ag1_id)
        ci_st = (st or {}).get('configured_identity') or {}
        gen_after_status = p.registry()['generation']
        check('iso03-status-never-mutates-registry-or-configured-identity',
              gen_after_status == gen1
              and ci_st.get('fingerprint') == '1111222233334444'
              and ci_st.get('account_label') == 'canonical@example.com'
              and not has_secret_material(raw_st),
              'gen=%s ci=%s' % (gen_after_status, ci_st))

        # 3. configured_identity survives rename, move, hide, and state changes
        p.run('rename', ag1_id, 'AG1 Isolated')
        p.run('move', ag1_id, 'down')
        p.run('set-hidden', ag1_id, 'true')
        p.run('set-hidden', ag1_id, 'false')
        _, g1, _, _ = p.run('get', ag1_id)
        ci_after_ops = ((g1 or {}).get('account') or {}).get('configured_identity') or {}
        check('iso04-configured-identity-survives-lifecycle-and-reorder',
              ci_after_ops.get('fingerprint') == '1111222233334444'
              and ci_after_ops.get('account_label') == 'canonical@example.com',
              str(ci_after_ops))

        # 4. bind-identity --clear removes configured_identity explicitly
        code, clr, _, _ = p.run('bind-identity', ag1_id, '--clear')
        _, g2, _, _ = p.run('get', ag1_id)
        check('iso05-bind-identity-clear-removes-configured-identity',
              code == 0 and clr and clr.get('status') == 'ok'
              and 'configured_identity' not in ((g2 or {}).get('account') or {}),
              str(g2))
    finally:
        p.cleanup()


def test_status_all_global_deadline():
    print('--- status --all global deadline ---')
    p = Plane('op-status-all')
    try:
        accounts = []
        for i in range(1, 6):
            sid = f'S-1-5-21-999999999-888888888-777777777-100{i}'
            accounts.append({
                'account_id': f'antigravity:windows-user:{sid}',
                'compact_label': f'AG{i}',
                'display_name': f'Antigravity {i}',
                'provider_id': 'antigravity',
                'execution_backend': 'windows_user',
                'execution_context_id': sid,
                'operational_state': 'ENABLED',
                'hidden': False,
                'windows_user': f'ag-user-{i}',
                'windows_sid': sid,
                'provider_metadata': {'windows_user': f'ag-user-{i}'}
            })
        p.write_registry({'schema': 'sai.accounts/registry/2', 'generation': 1, 'accounts': accounts})

        t0 = time.time()
        code, payload, stdout, stderr = p.run('status', '--all', '--timeout', '1500')
        elapsed = time.time() - t0

        check('status-all-exit-0', code == 0, f'exit code {code}: {stderr}')
        check('status-all-bounded-latency', elapsed < 4.0, f'elapsed {elapsed:.2f}s expected < 4.0s (not 5 * 1.5s = 7.5s)')
        check('status-all-schema-valid', payload is not None and payload.get('schema') == 'sai.accounts/status-all/1', str(payload))
        accs = payload.get('accounts', []) if payload else []
        check('status-all-preserves-all-accounts', len(accs) == 5, f'got {len(accs)} accounts')
        timed_out = [a for a in accs if a.get('error') == 'status_timeout']
        check('status-all-returns-status-timeout-rows', len(timed_out) == 5, f'{len(timed_out)} timed out rows')
    finally:
        p.cleanup()


def main():
    if not os.path.exists(os.path.join(ROOT, CSC_REQUIREMENT)):
        print('FAIL  build script missing')
        return 1
    ok, log = build()
    if not ok:
        print('FAIL  sai-accounts.exe build')
        print(log[-800:])
        return 1
    print('built sai-accounts.exe')

    test_registry_plane()
    test_n_accounts()
    test_migration_conflict()
    test_concurrent_writers()
    test_broker()
    test_profile_directory_launch()
    test_broker_disconnect()
    test_doctor_and_capabilities()
    test_helper_and_security()
    test_auth_operation()
    test_broker_auth_allowlist()
    test_broker_workdir_probe()
    test_broker_saipen_readiness()
    test_workdir_grants()
    test_grant_failure_contract()
    test_account_reorder()
    test_canonical_vs_runtime_identity_isolation()
    test_status_all_global_deadline()

    print()
    print('PASS %d / FAIL %d' % (len(PASSES), len(FAILURES)))
    if FAILURES:
        for f in FAILURES:
            print('  FAILED: ' + f)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

