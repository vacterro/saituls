"""Disposable real ConPTY bracketed-paste measurement (no clipboard access)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--exe', type=Path, required=True)
    p.add_argument('--deps', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--size', type=int, default=1024)
    p.add_argument('--shape', choices=['short', 'long', 'unicode'], default='short')
    p.add_argument('--raw', action='store_true', help='Disable paste summary in the disposable TUI')
    args = p.parse_args()
    sys.path.insert(0, str(args.deps.resolve()))
    from winpty import PtyProcess
    from winpty.enums import Backend

    root = args.evidence.resolve()
    root.mkdir(parents=True, exist_ok=False)
    home = root / 'home'
    config = home / '.config' / 'opencode'
    plugins = config / 'tui-modules'
    plugins.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name('native_paste_trace.js'), plugins / 'paste-trace.js')
    (config / 'tui.json').write_text(json.dumps({'plugin': [(plugins / 'paste-trace.js').as_uri()]}), encoding='utf-8')
    (config / 'opencode.json').write_text(json.dumps({
        'model': 'fixture/paste-fixture', 'enabled_providers': ['fixture'],
        'provider': {'fixture': {'npm': '@ai-sdk/openai-compatible',
            'name': 'Local paste fixture', 'options': {'baseURL': 'http://127.0.0.1:9/v1',
            'apiKey': 'disposable-fixture'}, 'models': {'paste-fixture': {
            'name': 'Paste fixture', 'limit': {'context': 1000000, 'output': 1024}}}}}
    }), encoding='utf-8')
    trace = root / 'trace.jsonl'
    env = os.environ.copy()
    for name in ('HOME', 'USERPROFILE'):
        env[name] = str(home)
    for name, folder in [('XDG_CONFIG_HOME', '.config'), ('XDG_DATA_HOME', 'data'),
                         ('XDG_CACHE_HOME', 'cache'), ('XDG_STATE_HOME', 'state'),
                         ('LOCALAPPDATA', 'local'), ('APPDATA', 'roaming')]:
        env[name] = str(home / folder)
    env['SAIPATCH_PASTE_TRACE'] = str(trace)
    env['SAIPATCH_PASTE_SUMMARY'] = 'false' if args.raw else 'true'
    env['OPENCODE_DISABLE_AUTOUPDATE'] = 'true'
    proc = PtyProcess.spawn([str(args.exe.resolve())], cwd=str(root), env=env,
                            dimensions=(35, 120), backend=Backend.ConPTY)
    output_bytes = 0
    startup = []
    pasting = False

    def drain():
        nonlocal output_bytes
        try:
            while proc.isalive():
                chunk = proc.read(65536)
                output_bytes += len(chunk.encode('utf-8'))
                if not pasting:
                    startup.append(chunk)
                    (root / 'startup-terminal.txt').write_text(''.join(startup), encoding='utf-8')
                # Answer cursor-position query, as a terminal would.
                if '\x1b[6n' in chunk:
                    proc.write('\x1b[1;1R')
        except (EOFError, OSError):
            pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    def records():
        if not trace.exists():
            return []
        return [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines() if line]

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rows = records()
            if predicate(rows):
                return rows
            if not proc.isalive():
                raise RuntimeError('TUI exited before measurement')
            time.sleep(.025)
        raise TimeoutError(f'No expected trace; output bytes={output_bytes}')

    try:
        wait_for(lambda rows: any(r['type'] == 'ready' for r in rows), 90)
        pasting = True
        unit = {'short': 'safe fixture line\n', 'long': 'safe fixture long line ',
                'unicode': 'Текст\t漢字 😀\r\n\n"C:\\safe\\fixture"\n'}[args.shape]
        payload = (unit * (args.size // len(unit) + 2))[:args.size]
        started = time.perf_counter()
        proc.write('\x1b[200~' + payload + '\x1b[201~')
        sent = time.perf_counter()
        rows = wait_for(lambda rows: any(r['type'] == 'paste' for r in rows), 60)
        normalized = payload.replace('\r\n', '\n').replace('\r', '\n')
        expected = normalized if args.raw else normalized.strip() + ' '
        expected_hash = hashlib.sha256(expected.encode()).hexdigest()
        paste = next(row for row in rows if row['type'] == 'paste')
        integrity = paste['sha256'] == expected_hash
        report = {'size': args.size, 'shape': args.shape, 'summary': not args.raw,
                  'integrity': integrity, 'write_ms': (sent-started)*1000,
                  'observed_ms': (time.perf_counter()-started)*1000,
                  'expected_sha256': expected_hash,
                  'trace': rows, 'output_bytes': output_bytes}
        (root / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report))
        if not integrity:
            raise AssertionError('Expanded composer differs from expected OpenCode semantics')
    finally:
        (root / 'startup-terminal.txt').write_text(''.join(startup), encoding='utf-8')
        proc.close(force=True)


if __name__ == '__main__':
    main()
