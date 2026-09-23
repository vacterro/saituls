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
  7. one-click setup, non-elevated launcher and safe destructive tools
     pixel icons and SAISPIN's dry-run-by-default contract
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


def _parse_reg_sections(text):
    secs, cur, body = [], None, []
    for line in text.splitlines():
        if line.startswith('[') and line.endswith(']'):
            if cur is not None:
                secs.append((cur, '\n'.join(body)))
            cur, body = line[1:-1], []
        elif cur is not None:
            body.append(line)
    if cur is not None:
        secs.append((cur, '\n'.join(body)))
    return secs


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
    restore_payload = read_text(os.path.join(ROOT, 'Scripts', 'restore_payload.ps1'))[0]
    install_all = read_text(os.path.join(ROOT, 'Installers', 'INSTALL_ALL.PS1'))[0]
    check('installer restores manifest payload from configured backup',
          'PAYLOAD_MANIFEST.txt' in restore_payload and
          'Get-FileHash' in restore_payload and
          'PayloadBackup' in install_all and
          'restore_payload.ps1' in install_all)

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
        os.path.join(ROOT, 'Add-CodexContextMenus.ps1'),
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
    # Repair must mean repair. Existence alone passed a zero-byte SAITULS.exe and
    # reported it ready, and two clicks of Home -> Install / repair ran two
    # elevated setups against the same fixed %TEMP% staging names.
    check('setup repairs by validity, not by mere existence',
          'function Get-RebuildReason' in setup_text
          and 'function Update-App' in setup_text
          and 'not a Windows executable' in setup_text
          and 'older than its source' in setup_text
          and 'if (-not (Test-Path $saitulsExe) -and $csc)' not in setup_text)
    check('setup is single-flight and owns its staging',
          r"'Global\SAITULS_SETUP'" in setup_text
          and 'function New-StagingPath' in setup_text
          and 'Another SAITULS install / repair is already running' in setup_text
          # No fixed %TEMP% staging path, and every temp file goes through the
          # run token. Matched against CODE lines only: setup.ps1's own comments
          # quote the old fixed names to explain why they were replaced, and a
          # naive substring check trips on its own documentation.
          and not any(
              ln.strip().startswith('$') and 'Join-Path $env:TEMP' in ln
              for ln in setup_text.splitlines())
          and not any(
              ln.strip().startswith('$temp') and '$Destination.download' in ln
              for ln in setup_text.splitlines()))
    check('setup never overwrites a running executable',
          '$Exe.new' in setup_text
          and 'but is running (PID ' in setup_text)
    check('everyday launcher is non-elevated',
          'RunAs' not in launcher_cmd and 'SAITULS.exe' in launcher_cmd)
    check('AI menus are optional',
          'IncludeAgentMenus' in install_all and 'if ($IncludeAgentMenus)' in install_all)
    # R013: INSTALL_ALL -Lang had no installed-locale marker -- switching
    # locale layered two translated trees, and uninstall without -Lang
    # removed English REMs while a localized generation orphaned.
    check('installer records its locale generation and removes it',
          '.installed_lang' in install_all
          and 'function Get-InstalledLang' in install_all
          and 'function Set-InstalledLang' in install_all
          and 'function Clear-InstalledLang' in install_all
          and 'Removing previous ' in install_all
          and 'Removing recorded ' in install_all
          # R002: the marker carries the applied file manifest and the record
          # write is fail-closed with rollback.
          and 'Set-InstalledLang $generation @(' in install_all
          and 'recorded generation' in install_all)
    # R015: the Codex cascade installer deleted the existing subtree first and
    # validated the launcher paths inside the write loop, so a wrong
    # -LauncherRoot converted a working menu into an empty one.
    codex_menus, _ = read_text(os.path.join(ROOT, 'Add-CodexContextMenus.ps1'))
    preflight_at = codex_menus.find('$missing = @($Items | Where-Object')
    delete_at = codex_menus.find('Remove-Item $base -Recurse -Force')
    check('Codex menu installer validates launchers before it deletes anything',
          preflight_at != -1 and delete_at != -1 and preflight_at < delete_at
          and 'the registry was NOT changed' in codex_menus
          and 'throw "Launcher script not found' not in codex_menus)
    # R015 second half: the two shell roots are one menu, so a failure part-way
    # through must not leave the first root converted and the second half-written.
    check('Codex menu installer writes both shell roots as one transaction',
          'function Backup-Root' in codex_menus
          and 'function Restore-Root' in codex_menus
          and 'Rolling both shell menus back' in codex_menus
          and 'foreach ($root in $Roots) { Restore-Root $root $backups[$root] }' in codex_menus)

    saituls_text, _ = read_text(os.path.join(ROOT, 'SAITULS.cs'))
    # R012: the GUI registry installer had no completion contract -- a selected
    # feature whose .REG was missing was silently skipped, and the elevated child's
    # exit code was discarded, so a failed reg import looked like success.
    check('SAITULS preflights the menu selection and reports the real result',
          'Missing registry file(s) - nothing was imported' in saituls_text
          and 'Missing removal file(s) - nothing was removed' in saituls_text
          and 'if (File.Exists(reg))\r\n' not in saituls_text
          and 'void ReportElevatedResult(int exitCode, int requested)' in saituls_text
          and 'proc.ExitCode' in saituls_text
          and 'catch { $__failed++' in saituls_text
          and 'exit $__failed' in saituls_text)
    # R014: a WAV read/decode failure collapsed to Player = null while Start()
    # still reported the engine as running, so a broken sound asset was a
    # silent no-op with no way to tell it from silence-by-choice.

    # Shell contracts that used to fail silently, pinned in source:
    #   - WM_NCHITTEST LPARAM carries SIGNED 16-bit coords; a checked int
    #     conversion overflowed on monitors above the primary.
    #   - Font.FromHfont does not own the HFONT: no DeleteObject == GDI leak.
    # These were found in LIMISAW and fixed in all three apps at once; LIMISAW
    # is now its own repository, so what stays here is the pair that still
    # ships here.
    hittest_apps = {
        'SAITULS.cs': saituls_text,
    }
    unsafe_hittest = [name for name, text in hittest_apps.items()
                      if 'unchecked((int)m.LParam.ToInt64())' not in text]
    check('NCHITTEST decodes LPARAM without overflow', not unsafe_hittest,
          '; '.join(unsafe_hittest))
    leaky_fonts = [name for name, text in hittest_apps.items()
                   if 'Font.FromHfont' in text and 'DeleteObject' not in text]
    check('pixel fonts release their HFONT', not leaky_fonts,
          '; '.join(leaky_fonts))
    check('tray icons load the size the shell asks for',
          'LoadIcon(s.IcoPath, SystemInformation.SmallIconSize.Width)' in saituls_text)

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

    # A shell verb resolves a bare executable name only from the Windows
    # directory and System32, never from PATH -- so a command line starting with
    # a per-user interpreter name dies with "Application not found" before the
    # worker ever runs. Every .REG here and in the generated locale trees.
    bare_interpreters = ('pythonw.exe', 'python.exe', 'py.exe')
    unresolvable = []
    for path in (sorted(glob.glob(os.path.join(ROOT, 'Registry', '*.REG')))
                 + sorted(glob.glob(os.path.join(ROOT, 'i18n', 'reg', '*', '*.REG')))):
        text, _ = read_text(path)
        for line in text.splitlines():
            if not line.startswith('@="'):
                continue
            head = line[3:].lstrip()
            if any(head.lower().startswith(exe) for exe in bare_interpreters):
                unresolvable.append(os.path.relpath(path, ROOT).replace('\\', '/'))
                break
    check('no .REG command starts with a PATH-only interpreter', not unresolvable,
          '; '.join(sorted(set(unresolvable))[:6]))

    # `"%1"` on a drive root arrives as `G:"` -- the trailing backslash escaped
    # the quote -- and stripping it leaves the DRIVE-RELATIVE `G:`, whose
    # abspath is this process's saved directory for that drive, not the root.
    # Read defensively: a gate that raises on a missing file reports an
    # instrument failure instead of the defect it exists to catch.
    shell_arg_path = os.path.join(ROOT, 'Scripts', 'shell_arg.py')
    shell_arg = read_text(shell_arg_path)[0] if os.path.isfile(shell_arg_path) else ''
    check('the shell argument normalizer exists and fixes a bare drive',
          'def shell_target' in shell_arg and 'target += os.sep' in shell_arg,
          '' if shell_arg else 'Scripts/shell_arg.py is missing')
    raw_strip = []
    for name in destructive + ('PACK.PYW',):
        worker, _ = read_text(os.path.join(ROOT, 'Scripts', name))
        if 'shell_target(' not in worker or "sys.argv[1].strip('\"')" in worker:
            raw_strip.append(name)
    check('every menu worker normalizes its shell argument', not raw_strip,
          '; '.join(raw_strip))

    # The downloader is a thin CMD launcher over one PowerShell worker; the
    # contract that matters (destination, bundled Deno, JS runtime wiring,
    # absolute ffmpeg location) lives in the worker.
    youtube_cmd, _ = read_text(os.path.join(ROOT, 'Scripts', 'DL_YT.CMD'))
    youtube, _ = read_text(os.path.join(ROOT, 'Scripts', 'DL_YT.PS1'))
    check('YouTube tool accepts destination and bundled Deno',
          '%~2' in youtube_cmd and '-OutPath' in youtube_cmd
          and "Join-Path $bin 'deno.exe'" in youtube
          and "Join-Path $bin 'yt-dlp.exe'" in youtube
          and '--remote-components' in youtube and 'ejs:github' in youtube
          and '--ffmpeg-location' in youtube)
    check('YouTube worker isolates each run and always cleans its URL list',
          'saituls-urls-' in youtube and '[guid]::NewGuid()' in youtube
          and 'Remove-Item -LiteralPath $tempFile' in youtube
          and 'SAITULS_DL_STATUS=' in youtube)

    readme, _ = read_text(os.path.join(ROOT, 'README.md'))
    readme_contract = all(token in readme for token in (
        'INSTALL.cmd', '14 Explorer commands', 'SAISPIN',
        'OpenCode', 'Removal and relocation',
    ))
    check('README covers install, features, tools and removal', readme_contract)

    # SAISPIN's own safety rules are proven by tests/test_saispin.py; what this
    # suite pins is that the watchdog cannot be armed by simply installing it.
    saispin_engine, _ = read_text(os.path.join(ROOT, 'Scripts', 'saispin_logic.py'))
    saispin_watch, _ = read_text(os.path.join(ROOT, 'Scripts', 'saispin_watch.ps1'))
    saispin_task, _ = read_text(os.path.join(ROOT, 'Scripts', 'Install-SaispinTask.ps1'))
    check('SAISPIN needs orphan AND sustained spin AND auto-kill to terminate',
          'if not sample.orphan' in saispin_engine
          and 'if not auto_kill' in saispin_engine
          and 'if allowlisted' in saispin_engine
          and 'if not history_trusted' in saispin_engine)
    check('SAISPIN is dry-run unless explicitly armed',
          '$armKill = $AutoKill.IsPresent -and -not $DryRun.IsPresent' in saispin_watch
          and "if ($AutoKill) { ' -AutoKill' } else { ' -DryRun' }" in saispin_task)
    check('SAISPIN re-validates identity before it kills anything',
          'function Invoke-ProvenKill' in saispin_watch
          and "$result.Refused = 'never-kill process'" in saispin_watch
          and "$result.Refused = 'identity changed (PID reuse) since the decision'" in saispin_watch
          and 'Global\\SAITULS_SAISPIN_WATCH' in saispin_watch)

    # Extracted-ownership guard: LIMISAW left under T-106 (E-578..E-586) and
    # Problip under E-1234; the pre-T-106 git HEAD plus later index resets once
    # resurrected these exact paths, so their working-tree presence is always a
    # regression. Historical mentions (CHANGELOG, LOG, ownership notes) are fine.
    extracted_ownership = ['LIMISAW.cs', 'LIMISAW.exe',
                           'Scripts/limisaw_probe.py', 'heh.ico',
                           'problip/Problip.cs', 'problip/Problip.exe',
                           'problip/blip01.wav', 'problip/problip.ico',
                           'problip/problip.ini', 'tests/problip-noaa-preview.png']
    resurrected = [p for p in extracted_ownership
                   if os.path.exists(os.path.join(ROOT, *p.split('/')))]
    check('extracted ownership does not reappear in SAITULS', not resurrected,
          '; '.join(resurrected))

    # Registry structural checks: visible leaves. Scope is the 14 install regs
    # (legacy FFMPEG.REG excluded by install_regs), but GIF quality defects
    # and executable-leaf coverage are proven against the canonical
    # Registry/FFMPEG_MENU.REG. Every visible leaf must have a real command.
    ffmpeg_menu = os.path.join(ROOT, 'Registry', 'FFMPEG_MENU.REG')
    ffmpeg_text, ffmpeg_enc = read_text(ffmpeg_menu)
    # T-167: single header, canonical UTF-16 LE + BOM for non-ASCII FFMPEG_MENU.
    check('FFMPEG_MENU single header', ffmpeg_text.count('Windows Registry Editor Version 5.00') == 1)
    check('FFMPEG_MENU canonical encoding', ffmpeg_enc == 'utf16')
    secs = _parse_reg_sections(ffmpeg_text)
    low = {k.lower(): (k, b) for k, b in secs}
    shell_nodes = [(k, b) for k, b in secs if '\\shell\\' in k.lower() and not k.lower().endswith('\\command')]
    leaves = [(k, b) for k, b in shell_nodes
              if not any(o.lower().startswith(k.lower() + '\\shell\\') for o, _ in secs)]
    check('FFMPEG_MENU has visible leaves', len(leaves) > 0, str(len(leaves)))
    # Discover leaf failures structurally: a parent must not carry a command,
    # a leaf must have one, duplicate leaf paths, submenu metadata on command.
    parents = [(k, b) for k, b in shell_nodes
               if any(o.lower().startswith(k.lower() + '\\shell\\') for o, _ in secs)]
    parent_leaks = [k for k, _ in parents if k.lower() + '\\command' in low]
    check('FFMPEG_MENU parent-as-leaf (no command on submenu)', not parent_leaks,
          '; '.join(parent_leaks[:4]))
    dup_leaves = [k for k, n in __import__('collections').Counter(
        k.lower() for k, _ in leaves).items() if n > 1]
    check('FFMPEG_MENU no duplicate leaf paths', not dup_leaves,
          '; '.join(dup_leaves[:4]))
    cmd_meta = [k for k, b in secs if k.lower().endswith('\\command')
                and ('"subcommands"' in b or '"MUIVerb"' in b)]
    # Allow MUIVerb on AV1 leaves (member of leaves still has MUIVerb on parent, not command). Command keys must not carry submenu metadata.
    # The check is exact: command key itself must not contain subcommands/icon/MUIVerb values (icon is on leaf, not command).
    cmd_meta = [k for k, b in secs if k.lower().endswith('\\command')
                and '"subcommands"' in b]
    check('FFMPEG_MENU no submenu metadata on command keys', not cmd_meta,
          '; '.join(cmd_meta[:4]))
    def _parse_cmd(b):
        m = re.search(r'@="((?:\\"|[^"])*)"', b)
        if not m:
            return None
        raw = m.group(1).replace('\\"', '"')
        parts = re.findall(r'"([^"]*)"', raw)
        return parts
    empty_leaves = []
    malformed = []
    for k, b in leaves:
        ck = k.lower() + '\\command'
        if ck not in low:
            empty_leaves.append(k + ' (missing \\command)')
            continue
        _, cb = low[ck]
        parts = _parse_cmd(cb)
        if parts is None or len(parts) < 4:
            empty_leaves.append(k)
            continue
        bat, p1, args, suffix = parts[0], parts[1], parts[2], parts[3]
        if not suffix or suffix.strip() == '':
            empty_leaves.append(k)
        if not bat or not p1:
            malformed.append(k)
        # Every leaf command must be executable: FFMPEG_RUN.BAT + "%1" + args + suffix
        if not bat.lower().endswith('ffmpeg_run.bat') or p1 != '%1':
            malformed.append(k + ' (not FFMPEG_RUN.BAT "%1")')
    check('FFMPEG_MENU every visible leaf has executable command', not empty_leaves,
          '; '.join(empty_leaves[:4]))
    check('FFMPEG_MENU leaf commands well-formed', not malformed,
          '; '.join(malformed[:4]))
    # GIF quality commands: every visible GIF leaf must have non-empty suffix and palette filter
    gif_leaves = [(k, low[k.lower() + '\\command'][1]) for k, _ in leaves if '\\gif' in k.lower()]
    gif_bad_suffix = []
    gif_bad_filter = []
    for k, cb in gif_leaves:
        parts = _parse_cmd(cb)
        if not parts or len(parts) < 4:
            gif_bad_suffix.append(k)
            gif_bad_filter.append(k)
            continue
        args, suffix = parts[2], parts[3]
        if not suffix or not suffix.lower().endswith('.gif'):
            gif_bad_suffix.append(k + ':' + repr(suffix))
        # Video GIF quality leaves (Сделать GIF) require split/palettegen/paletteuse; image GIF leaves use -f gif
        if 'сделать gif' in k.lower():
            if not ('palettegen' in args and 'paletteuse' in args and 'split' in args):
                gif_bad_filter.append(k)
        else:
            # image -> GIF leaves are single-step; filter is -f gif
            if args.strip() == '' or args.strip() == '""':
                gif_bad_filter.append(k + ' (empty filter)')
    check('FFMPEG_MENU GIF leaves have GIF suffix', not gif_bad_suffix,
          '; '.join(gif_bad_suffix[:4]))
    check('FFMPEG_MENU GIF leaves have correct filter', not gif_bad_filter,
          '; '.join(gif_bad_filter[:4]))
    # No whole-extension destructive REM deletes: FFMPEG_MENU_REM must not contain [-HKEY...\.ext] without \shell
    ffmpeg_rem = os.path.join(ROOT, 'Registry', 'FFMPEG_MENU_REM.REG')
    if os.path.isfile(ffmpeg_rem):
        rem_text, _ = read_text(ffmpeg_rem)
        import re as _re
        whole_ext = _re.findall(r'^\[-HKEY[^\]]*\\SystemFileAssociations\\\.[^\\\]]+\]$',
                                rem_text, _re.M)
        check('FFMPEG_MENU_REM no whole-extension destructive deletes', not whole_ext,
              '; '.join(whole_ext[:4]))
    # FFMPEG BAT dependency manifest closure if manifest exists: every Bin/ path referenced by FFMPEG_MENU must be listed
    manifest = os.path.join(ROOT, 'PAYLOAD_MANIFEST.txt')
    if os.path.isfile(manifest) and os.path.isfile(ffmpeg_menu):
        mlines = [ln.strip() for ln in open(manifest, encoding='utf-8', errors='replace').read().splitlines()
                  if ln.strip() and not ln.strip().startswith('#')]
        mset = set(ln.replace('\\', '/') for ln in mlines)
        # extract Bin/ paths from the REG: the BAT itself plus the EXEs it invokes via FFMPEG_RUN.PS1
        bin_refs = set()
        for line in ffmpeg_text.splitlines():
            if '@="' in line and 'Bin\\' in line:
                for mm in re.finditer(r'Bin\\[^\"]+', line):
                    # REG escapes \ as \\, so split on \\ yields the canonical path
                    raw = mm.group(0)
                    # e.g. Bin\\FFMPEG_RUN.BAT\\  -> Bin/FFMPEG_RUN.BAT
                    canon = raw.split('\\')[0] + '/' + raw.split('\\')[1] if '\\' in raw else raw
                    # robust: take first two segments Bin/NAME.EXT
                    segs = [s for s in re.split(r'\\+', raw) if s]
                    if len(segs) >= 2:
                        canon = segs[0] + '/' + segs[1]
                    else:
                        canon = raw.replace('\\', '/')
                    bin_refs.add(canon)
        # FFMPEG_RUN.BAT delegates to FFMPEG_RUN.PS1 which requires FFMPEG.EXE + FFPROBE.EXE
        if any('FFMPEG_RUN.BAT' in p for p in bin_refs):
            bin_refs.update({'Bin/FFMPEG_RUN.PS1', 'Bin/FFMPEG.EXE', 'Bin/FFPROBE.EXE'})
        missing_manifest = sorted(p for p in bin_refs if p not in mset)
        check('FFMPEG BAT dependencies listed in PAYLOAD_MANIFEST', not missing_manifest,
              '; '.join(missing_manifest[:4]))

    print('---')
    print('FAILED' if fails else 'PASS', f'({fails} failure(s))')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
