#!/usr/bin/env python3
"""Integrity suite for the context-menu reg/i18n tree.

Run:  python tests/test_regs.py
Exit: 0 = all PASS, 1 = failures.

  Checks:
  1. install-set .REG parse (file header), count == 14
  2. encoding rule: non-ASCII .REG must be UTF-16 LE + BOM; ASCII any
  3. every drive path referenced by install regs / scripts / bin wrappers resolves
  4. i18n: 33 locale string files, identical key set (parity)
  5. reg-map.json: every mapped label key exists in the bundle; no empty mappings
  6. AI-agent PowerShell launch/install scripts parse and self-test both YOLO commands
  7. one-click setup, non-elevated launcher, safe destructive tools and LIMISAW UX invariants
"""
import re, os, glob, io, json, subprocess, sys

ROOT = os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.sep + '..')


def read_text(p):
    b = open(p, 'rb').read()
    if b[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return b.decode('utf-16'), 'utf16'
    try:
        return b.decode('utf-8'), 'utf8'
    except UnicodeDecodeError:
        return b.decode('cp1251', errors='replace'), 'ansi'


def extract_paths(text, root):
    t = text.replace('\\\\', '\\').replace('%%ROOT%%', root)
    pat = re.compile(r'[A-Za-z]:\\[\w\\\.\- ()&+]+?\.(?:exe|cmd|bat|pyw|ico|ps1|ini|py)\b',
                     re.IGNORECASE)
    return pat.findall(t)


def install_regs():
    regs = glob.glob(os.path.join(ROOT, 'Registry', '*.REG'))
    return [p for p in regs
            if not re.search(r'_REM|_UTF16|_REWRITTEN|FFMPEG\.REG$',
                             os.path.basename(p), re.I)]


def main():
    fails = 0

    def check(name, ok, detail=''):
        nonlocal fails
        print(('PASS  ' if ok else 'FAIL  ') + name
              + ('  ' + detail if detail else ''))
        if not ok:
            fails += 1

    regs = install_regs()
    check('install-set size == 14', len(regs) == 14, str(len(regs)))

    all_paths = []
    hardcoded = []
    for p in regs:
        text, enc = read_text(p)
        ok_parse = text.startswith('Windows Registry Editor Version 5.00')
        check('parse  ' + os.path.basename(p), ok_parse)
        non_ascii = any(ord(c) > 127 for c in text)
        ok_enc = (not non_ascii) or (enc == 'utf16')
        check('encode ' + os.path.basename(p), ok_enc,
              enc + ('/non-ascii' if non_ascii else '/ascii'))
        all_paths += extract_paths(text, ROOT)
        if '__SAITULS' in text:
            hardcoded.append(os.path.basename(p))
    check('install regs relocation-proof (no hardcoded root)', not hardcoded,
          '; '.join(hardcoded))

    for p in (glob.glob(os.path.join(ROOT, 'Scripts', '*.*'))
              + glob.glob(os.path.join(ROOT, 'Bin', '*.BAT'))
              + glob.glob(os.path.join(ROOT, '*.cmd'))
              + glob.glob(os.path.join(ROOT, 'Bin', 'App', '*.*'))):
        if os.path.isdir(p):
            continue
        text, enc = read_text(p)
        all_paths += extract_paths(text, ROOT)

    seen = set()
    missing = []
    payload_missing = []
    payload_exts = {'.exe', '.dll'}
    for p in all_paths:
        if p in seen:
            continue
        seen.add(p)
        if os.path.exists(p):
            continue
        try:
            rel = os.path.relpath(p, ROOT).replace('\\', '/')
        except ValueError:
            # cross-volume path (e.g. C: vs V:); treat as non-payload.
            rel = None
        if rel is not None and rel.startswith('Bin/') and os.path.splitext(p)[1].lower() in payload_exts:
            payload_missing.append(rel)
        elif 'Program Files' in p:
            # build tool discovered by BUILD_PAYLOAD.cmd (7-Zip, with tar
            # fallback) — external, optional, not a SAITULS dependency.
            continue
        else:
            missing.append(p)
    check('source paths referenced resolve', not missing,
          '; '.join(missing[:5]) or str(len(seen)) + ' distinct')
    check('optional payload manifest', True,
          str(len(payload_missing)) + ' heavy binaries supplied by release payload')

    sfiles = sorted(glob.glob(os.path.join(ROOT, 'i18n', 'strings', 'strings.*.json')))
    ds = [json.load(io.open(f, encoding='utf-8')) for f in sfiles]
    check('i18n locales == 33', len(ds) == 33, str(len(ds)))
    if ds:
        keys = [set(d.keys()) for d in ds]
        check('i18n key parity', all(k == keys[0] for k in keys),
              str(len(keys[0])) + ' keys')
        bundle = next((d for f, d in zip(sfiles, ds)
                       if os.path.basename(f) == 'strings.en.json'), ds[0])
    else:
        bundle = {}

    rm = json.load(io.open(os.path.join(ROOT, 'i18n', 'reg-map.json'), encoding='utf-8'))
    bad_keys = [regname + ':' + key
                for regname, mapping in rm.items()
                for label, key in mapping.items() if key not in bundle]
    check('reg-map keys exist in bundle', not bad_keys, '; '.join(bad_keys[:5]))
    empty_map = [n for n, m in rm.items() if not m]
    check('reg-map no empty mappings', not empty_map, '; '.join(empty_map))

    agent_scripts = [
        os.path.join(ROOT, 'Scripts', 'AI_AGENT_LAUNCHER.PS1'),
        os.path.join(ROOT, 'Installers', 'INSTALL_AI_AGENT_MENUS.PS1'),
        os.path.join(ROOT, 'Add-ClineContextMenu.ps1'),
        os.path.join(ROOT, 'Add-OpenCodeContextMenu.ps1'),
        os.path.join(ROOT, 'Remove-AgentContextMenus.ps1'),
        os.path.join(ROOT, 'setup.ps1'),
        os.path.join(ROOT, 'Installers', 'INSTALL_ALL.PS1'),
    ]
    for script in agent_scripts:
        escaped = script.replace("'", "''")
        parse = subprocess.run(
            [
                'powershell.exe', '-NoLogo', '-NoProfile', '-Command',
                "$t=$null;$e=$null;"
                f"[Management.Automation.Language.Parser]::ParseFile('{escaped}',"
                "[ref]$t,[ref]$e)|Out-Null;if($e.Count){$e;exit 1}",
            ],
            capture_output=True,
            text=True,
        )
        check('PowerShell parse ' + os.path.basename(script), parse.returncode == 0,
              (parse.stderr or parse.stdout).strip())

    launcher = agent_scripts[0]
    expected = {
        'OpenCode': {'--auto'},
        'Cline': {'--auto-approve', 'true', '--tui'},
    }
    for agent, required_args in expected.items():
        probe = subprocess.run(
            [
                'powershell.exe', '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', launcher, '-Agent', agent, '-WorkDir', ROOT, '-SelfTest',
            ],
            capture_output=True,
            text=True,
        )
        try:
            result = json.loads(probe.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            result = {}
        ok = (
            probe.returncode == 0
            and required_args.issubset(set(result.get('Arguments', [])))
            and result.get('ProjectFirst') is True
            and result.get('TitleGuard') is True
            and result.get('VintageSkill') is True
        )
        check('AI launcher ' + agent + ' YOLO/title/Wintage', ok,
              (probe.stderr or probe.stdout).strip())

    installer_text, _ = read_text(agent_scripts[1])
    console_contract = all(token in installer_text for token in (
        'ForceV2', 'CtrlKeyShortcutsDisabled', 'InterceptCopyPaste',
        'WintagePalette', 'goldendefault',
    ))
    check('AI console Ctrl+C/V + Wintage contract', console_contract)

    setup_text, _ = read_text(os.path.join(ROOT, 'setup.ps1'))
    install_cmd, _ = read_text(os.path.join(ROOT, 'INSTALL.cmd'))
    launcher_cmd, _ = read_text(os.path.join(ROOT, 'SAITULS_LAUNCHER.cmd'))
    install_all, _ = read_text(os.path.join(ROOT, 'Installers', 'INSTALL_ALL.PS1'))
    one_click_tokens = (
        'setup.ps1', '-NoLaunch',
        'python.org/ftp/python', 'ffmpeg-release-essentials.zip',
        'releases/latest/download/yt-dlp.exe', 'aria2-1.37.0-win-64bit-build1.zip',
        'deno-x86_64-pc-windows-msvc.zip',
    )
    check('one-click installer provisions every required runtime',
          all(token in install_cmd + setup_text for token in one_click_tokens)
          and 'Choice (1/2/3)' not in setup_text)
    check('everyday launcher is non-elevated',
          'RunAs' not in launcher_cmd and 'SAITULS.exe' in launcher_cmd)
    check('AI menus are optional',
          'IncludeAgentMenus' in install_all and 'if ($IncludeAgentMenus)' in install_all)

    limisaw_text, _ = read_text(os.path.join(ROOT, 'LIMISAW.cs'))
    limisaw_ux = all(token in limisaw_text for token in (
        'TrayMetric', '"lowest"', 'p.X < Width - 22',
        r'Local\\LimisawApp', r'Local\\LimisawShow',
        'tray.DoubleClick', 'HideToTray',
    )) and 'TrayC1_5h' not in limisaw_text and '2x2 grid' not in limisaw_text
    check('LIMISAW one-number tray, drag and single-instance contract', limisaw_ux)

    saituls_text, _ = read_text(os.path.join(ROOT, 'SAITULS.cs'))
    check('SAITULS hidden instance can be restored',
          r'Local\\SaitulsApp' in saituls_text and r'Local\\SaitulsShow' in saituls_text)

    destructive = ('DEL_DUP.PYW', 'DEL_EMPTY.PYW', 'DEL_JUNK.PYW', 'DEL_SAME.PYW')
    missing_confirmation = []
    for name in destructive:
        worker, _ = read_text(os.path.join(ROOT, 'Scripts', name))
        if 'messagebox.askyesno' not in worker:
            missing_confirmation.append(name)
    check('destructive workers require confirmation', not missing_confirmation,
          '; '.join(missing_confirmation))

    youtube, _ = read_text(os.path.join(ROOT, 'Scripts', 'DL_YT.CMD'))
    check('YouTube tool accepts destination and bundled Deno',
          'set "outpath=%~2"' in youtube and 'Bin\\deno.exe' in youtube
          and '--remote-components ejs:github' in youtube)

    readme, _ = read_text(os.path.join(ROOT, 'README.md'))
    readme_contract = all(token in readme for token in (
        'INSTALL.cmd', '14 Explorer commands', 'LIMISAW', 'Problip',
        'OpenCode', 'Removal and relocation', 'read `auth.json`',
    ))
    check('README covers install, features, limits and removal', readme_contract)

    print('---')
    print('FAILED' if fails else 'PASS', f'({fails} failure(s))')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
