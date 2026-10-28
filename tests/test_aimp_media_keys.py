#!/usr/bin/env python3
"""T-191 oracle: hardware media keys route DELIBERATELY to AIMP in ___AHK/___MAIN.ahk.

Run:  python tests/test_aimp_media_keys.py
Exit: 0 = all PASS (or SKIP when ___AHK/___MAIN.ahk is absent -- it is
      gitignored, so CI never has it), 1 = failures.

  Checks (static, on the live script):
  1. Media_Play_Pause/Next/Prev/Stop each bound exactly once, globally (no #If),
     to AIMPMediaKey with the Winamp WM_COMMAND id 40046/40048/40044/40047
  2. AIMPMediaKey targets AIMP by process identity (ahk_class Winamp v1.x +
     ahk_exe AIMP.exe) via PostMessage WM_COMMAND, keeps DetectHiddenWindows On
     THROUGH the PostMessage (AIMP's IPC window is hidden; in v1 ahk_id does not
     see a hidden top-level window), and re-sends the key only when not posted
  3. no generic Send {Media_*} anywhere else (that goes to whichever Windows
     media session owns SMTC -- the regression)
  4. Volume_Up/Down/Mute bindings are byte-identical to the pre-T-191 script
  5. the script parses under the installed AHK v1 interpreter (/iLib NUL)
  Mutants (must each FAIL the oracle): generic Media_* re-send; restoring
  DetectHiddenWindows before the PostMessage; key swallowed when not posted.
"""
import os, re, subprocess, sys, tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
MAIN = os.path.join(ROOT, '___AHK', '___MAIN.ahk')
AHK = r'C:\Program Files\AutoHotkey\AutoHotkeyU64.exe'

MEDIA = {'Media_Play_Pause': 40046, 'Media_Next': 40048,
         'Media_Prev': 40044, 'Media_Stop': 40047}

# Pre-T-191 volume block, verbatim (unchanged since the June ___AHK.rar baseline).
VOLUME = [
    'Volume_Mute::   Run, nircmd.exe monitor off',
    '^Volume_Mute::  Run, sndvol.exe',
    '+^Volume_Mute:: Run, mmsys.cpl',
    'Volume_Up::\n    Send, {Ctrl Down}{LShift Down}={Ctrl Up}{LShift Up}\nreturn',
    'Volume_Down::\n    Send, {Ctrl Down}{LShift Down}-{Ctrl Up}{LShift Up}\nreturn',
]


def func_body(text, name):
    m = re.search(r'^' + name + r'\([^)]*\)\s*\{\n(.*?)^\}', text, re.M | re.S)
    return m.group(1) if m else None


def active_if(text, pos):
    """The #If expression in force at `pos` ('' = global)."""
    cur = ''
    for m in re.finditer(r'^#If(?:WinActive|WinExist|WinNotActive|WinNotExist)?\b(.*)$',
                         text[:pos], re.M):
        cur = m.group(1).strip()
    return cur


def oracle(text):
    errs = []
    code = '\n'.join(l for l in text.splitlines() if not l.lstrip().startswith(';'))

    for key, cid in MEDIA.items():
        hits = list(re.finditer(r'^[~$*<>^!+#]*' + key + r'\s*::(.*)$', code, re.M))
        if len(hits) != 1:
            errs.append('%s bound %d times (want 1)' % (key, len(hits)))
            continue
        h = hits[0]
        if active_if(code, h.start()):
            errs.append('%s is inside an #If context' % key)
        if not re.fullmatch(r'\s*AIMPMediaKey\(%d,\s*"%s"\)\s*' % (cid, key), h.group(1)):
            errs.append('%s handler is not AIMPMediaKey(%d, "%s"): %r'
                        % (key, cid, key, h.group(1).strip()))

    body = func_body(code, 'AIMPMediaKey')
    if body is None:
        errs.append('AIMPMediaKey() missing')
    else:
        lines = [l.strip() for l in body.splitlines() if l.strip()]
        idx = lambda pat: next((i for i, l in enumerate(lines) if re.search(pat, l, re.I)), -1)
        on = idx(r'^DetectHiddenWindows,\s*On$')
        find = idx(r'ahk_class Winamp v1\.x ahk_exe AIMP\.exe')
        post = idx(r'^PostMessage,\s*0x0111,\s*%cmdId%.*ahk_id %hwnd%')
        ok = idx(r'posted\s*:=\s*!ErrorLevel')
        restore = idx(r'^DetectHiddenWindows,\s*%prevDetect%$')
        send = idx(r'^Send,\s*\{%passthroughKey%\}$')
        guard = idx(r'^if\s*\(\s*!posted\s*\)$')
        if min(on, find, post, ok, restore, send, guard) < 0:
            errs.append('AIMPMediaKey missing a required step (on=%d find=%d post=%d '
                        'ok=%d restore=%d guard=%d send=%d)'
                        % (on, find, post, ok, restore, guard, send))
        else:
            if not on < find < post < ok < restore:
                errs.append('DetectHiddenWindows must stay On through the PostMessage')
            if not (guard < send and guard + 1 == send):
                errs.append('fallback Send must run only when the post did not happen')

    generic = [m.group(0) for m in re.finditer(r'Send\w*,?\s*\{Media_\w+\}', code)]
    if generic:
        errs.append('generic media re-send found: %s' % generic)

    for v in VOLUME:
        if text.count(v) != 1:
            errs.append('volume binding changed/duplicated: %r' % v.splitlines()[0])
    if len(re.findall(r'^[~$*<>^!+#]*Volume_\w+\s*::', code, re.M)) != len(VOLUME):
        errs.append('unexpected extra Volume_* binding')
    return errs


def mutants(text):
    body = func_body(text, 'AIMPMediaKey')
    yield 'generic Media_* re-send', text.replace(
        'Media_Play_Pause::AIMPMediaKey(40046, "Media_Play_Pause")',
        'Media_Play_Pause::Send, {Media_Play_Pause}')
    # the bug fixed in T-191 BUILD: detection restored before the post
    lines = body.splitlines()
    r = next(i for i, l in enumerate(lines) if '%prevDetect%' in l)
    moved = lines[:r] + lines[r + 1:]
    w = next(i for i, l in enumerate(moved) if 'WinGet, hwnd' in l)
    moved.insert(w + 1, lines[r])
    yield 'hidden-window detection off at PostMessage', text.replace(body, '\n'.join(moved) + '\n')
    yield 'key swallowed when not posted', text.replace(
        '    if (!posted)\n        Send, {%passthroughKey%}', '    return')


def parse_ok(path):
    if not os.path.exists(AHK):
        return None
    r = subprocess.run([AHK, '/ErrorStdOut', '/iLib', 'NUL', path],
                       cwd=os.path.dirname(MAIN), capture_output=True, timeout=60)
    return r.returncode == 0, r.stderr.decode('utf-8', 'replace').strip()


def main():
    if not os.path.exists(MAIN):
        print('SKIP ___AHK/___MAIN.ahk not present (gitignored, local-only)')
        return 0
    text = open(MAIN, encoding='utf-8-sig').read().replace('\r\n', '\n')
    fails = 0

    errs = oracle(text)
    for e in errs:
        print('FAIL', e)
    fails += len(errs)
    if not errs:
        print('PASS live ___MAIN.ahk routes Media_* to AIMP; volume keys unchanged')

    for name, m in mutants(text):
        if m == text:
            print('FAIL mutant %r did not apply' % name); fails += 1
        elif oracle(m):
            print('PASS mutant rejected: %s' % name)
        else:
            print('FAIL mutant NOT rejected: %s' % name); fails += 1

    res = parse_ok(MAIN)
    if res is None:
        print('SKIP AHK v1 interpreter not installed; parse check skipped')
    else:
        ok, err = res
        print(('PASS' if ok else 'FAIL') + ' AHK v1 parse of ___MAIN.ahk' + ('' if ok else ': ' + err))
        fails += not ok
        fd, bad = tempfile.mkstemp(suffix='.ahk', dir=os.path.dirname(MAIN))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text.replace('posted := !ErrorLevel', 'posted := !ErrorLevel)'))
            bok, _ = parse_ok(bad)
        finally:
            os.remove(bad)
        print(('PASS' if not bok else 'FAIL') + ' parse check rejects a planted syntax error')
        fails += bok

    print('\n%s: %d failure(s)' % ('PASS' if not fails else 'FAIL', fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
