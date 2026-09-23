#!/usr/bin/env python3
"""Generate localized .reg variants from i18n/strings/<locale>.json.

Usage:
    python gen_locale_reg.py et [--out i18n/reg/et]

Decision (T-019): installer-side label substitution. Per-locale .reg
variants would mean 13 files x 33 locales = 429 files (FFMPEG_MENU.REG
alone is 257KB -> ~8MB of near-duplicate bytes). Instead the bundle is the
single source of truth and this generator produces one locale's variants
on demand; INSTALL_ALL.PS1 -Lang imports them.

Substitution is a plain global text replace per label, so key names
(shell\\Конвертировать), default values (@=), MUIVerb values, and comment
lines all stay consistent -- nested command-key paths keep their parent
labels in sync because the same string is replaced everywhere.

Encoding rule (reg.exe): Cyrillic / any non-ASCII MUST be UTF-16 LE + BOM,
else reg.exe garbles it. Pure-ASCII output keeps the source file's
original encoding (ASCII regs stay ASCII, UTF-16 sources stay UTF-16).

Publication is fail-closed (T-137). An install .reg must carry every label
its reg-map entry claims; a removal counterpart deletes ASCII key names in
most files and localized ones in a few, so it must carry either the whole
parent set or none of it -- a partial set means the two files drifted.
Any violated requirement, or a mapped label whose bundle key is gone, is
an error: nothing is written, so a half-substituted generation can never
reach the destination. The whole locale is rendered in memory, staged in a
fresh directory on the destination volume, and swapped in by rename, which
removes the mixed generation a mid-loop crash used to leave behind.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # project root
I18N = ROOT / 'i18n'
REGISTRY = ROOT / 'Registry'
EXCLUDE = re.compile(r'_(REM|UTF16|REWRITTEN)|^FFMPEG\.REG$', re.I)

UTF16_BOMS = (b'\xff\xfe', b'\xfe\xff')


def read_reg(p: Path):
    raw = p.read_bytes()
    if raw[:2] in UTF16_BOMS:
        return raw.decode('utf-16'), 'utf-16'
    # ASCII regs in this tree are cp1251-safe (labels are ASCII), but
    # decode leniently and keep bytes if pure ASCII at write time.
    return raw.decode('cp1251', errors='replace'), 'ascii'


def encode_reg(text: str, src_enc: str) -> bytes:
    """UTF-16 LE + BOM if any non-ASCII, else match source encoding."""
    non_ascii = any(ord(c) > 127 for c in text)
    if non_ascii or src_enc == 'utf-16':
        return b'\xff\xfe' + text.encode('utf-16-le')
    return text.encode('cp1251')


def required_labels(name: str, mapping: dict, text: str) -> set:
    """Which mapped labels this file MUST contain.

    Install regs own their labels, so all of them are mandatory. A removal
    counterpart is all-or-nothing: it either deletes localized key names
    (MKV_FIX_REM.REG) and needs the full parent set, or it deletes ASCII
    ones and needs none of it. Anything between the two is drift.
    """
    if not name.upper().endswith('_REM.REG'):
        return set(mapping)
    return set(mapping) if any(label in text for label in mapping) else set()


def render(name: str, text: str, mapping: dict, strings: dict):
    """Substitute one file. Returns (text, replaced, errors)."""
    required = required_labels(name, mapping, text)
    source = text
    replaced, errors = {}, []
    # longest label first: a label that is a substring of another
    # (e.g. future 'Сжать в MP4' vs 'Сжать в MP4 (HD)') must win
    for label, key in sorted(mapping.items(),
                             key=lambda kv: len(kv[0]), reverse=True):
        # presence is judged against the ORIGINAL text: a nested label
        # ('Видео' inside 'Видео (плейлист)') is still present in the file
        # even once the longer label has consumed its occurrences
        if label not in source:
            if label in required:
                errors.append(f'{name}: required label absent: {label!r}')
            continue
        if key not in strings:
            errors.append(f'{name}: bundle key absent: {key} (for {label!r})')
            continue
        # the count is of substitutions actually PERFORMED, so it is taken
        # from the text as it stands now, not from the original
        n = text.count(label)
        if not n:
            continue
        replaced[label] = n
        text = text.replace(label, strings[key])
    return text, replaced, errors


def publish(out_dir: Path, rendered):
    """Stage the whole generation, then swap it in by rename.

    rendered: list of (name, bytes). The stage directory is created inside
    the destination's parent, so it is on the same volume and the rename is
    a rename rather than a copy; it is validated as fresh and empty before
    a single byte is written into it.
    """
    out_dir = out_dir.resolve()
    if out_dir.exists() and not out_dir.is_dir():
        raise NotADirectoryError(f'destination is not a directory: {out_dir}')
    parent = out_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.gen-', dir=parent))
    try:
        if any(stage.iterdir()):
            raise OSError(f'staging directory is not empty: {stage}')
        for name, data in rendered:
            (stage / name).write_bytes(data)
        previous = None
        if out_dir.exists():
            previous = parent / (out_dir.name + '.old-' + stage.name)
            os.rename(out_dir, previous)
        try:
            os.rename(stage, out_dir)
        except OSError:
            if previous is not None:
                os.rename(previous, out_dir)     # put the old one back
            raise
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='gen_locale_reg.py',
        description='Generate localized .reg variants for one locale.')
    parser.add_argument('locale', nargs='?', default='et',
                        help='locale of i18n/strings/strings.<locale>.json')
    parser.add_argument('--out', metavar='PATH', type=Path,
                        help='destination directory (default i18n/reg/<locale>)')
    args = parser.parse_args(argv)               # unknown argument -> exit 2

    bundle = I18N / 'strings' / f'strings.{args.locale}.json'
    if not bundle.is_file():
        sys.exit(f'gen_locale_reg: no string bundle: {bundle}')
    # utf-8-sig: a bundle re-saved by Notepad or by PowerShell -Encoding UTF8
    # carries a BOM, and refusing to parse it would be a hostile way to fail.
    strings = json.loads(bundle.read_text(encoding='utf-8-sig'))
    reg_map = json.loads((I18N / 'reg-map.json').read_text(encoding='utf-8-sig'))
    out_dir = args.out if args.out is not None else I18N / 'reg' / args.locale

    # install set + every uninstall counterpart: -Lang must cover BOTH
    # install and uninstall, or localized keys (renamed shell keys) can
    # never be removed (reviewer finding, T-019)
    install = [p for p in sorted(REGISTRY.glob('*.REG'))
               if not EXCLUDE.search(p.name)]
    uninstall = sorted(REGISTRY.glob('*_REM.REG'))
    rendered, report, errors = [], [], []
    for p in install + uninstall:
        # map lookup: exact name, else the base install reg it removes
        mapping = reg_map.get(p.name) or reg_map.get(p.name.replace('_REM', ''), {})
        text, enc = read_reg(p)
        text, replaced, file_errors = render(p.name, text, mapping, strings)
        errors += file_errors
        rendered.append((p.name, encode_reg(text, enc)))
        report.append(f'  {p.name}: {len(replaced)} labels '
                      f'({", ".join(f"{k} x{n}" for k, n in replaced.items()) or "none"})')

    if errors:
        for error in errors:
            print(f'  !! {error}', file=sys.stderr)
        sys.exit(f'gen_locale_reg: {len(errors)} substitution error(s); '
                 f'nothing published to {out_dir}')

    publish(out_dir, rendered)
    print('\n'.join(report))
    print(f'-> {out_dir} ({len(rendered)} files)')


if __name__ == '__main__':
    main()
