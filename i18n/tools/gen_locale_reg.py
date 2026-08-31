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
"""
import json
import re
import sys
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


def write_reg(p: Path, text: str, src_enc: str):
    """UTF-16 LE + BOM if any non-ASCII, else match source encoding."""
    non_ascii = any(ord(c) > 127 for c in text)
    if non_ascii:
        p.write_bytes(b'\xff\xfe' + text.encode('utf-16-le'))
    elif src_enc == 'utf-16':
        p.write_bytes(b'\xff\xfe' + text.encode('utf-16-le'))
    else:
        p.write_bytes(text.encode('cp1251'))


def main(locale: str, out_dir: Path):
    strings = json.loads((I18N / 'strings' / f'strings.{locale}.json')
                         .read_text(encoding='utf-8'))
    reg_map = json.loads((I18N / 'reg-map.json').read_text(encoding='utf-8'))

    out_dir.mkdir(parents=True, exist_ok=True)
    report = []
    # install set + every uninstall counterpart: -Lang must cover BOTH
    # install and uninstall, or localized keys (renamed shell keys) can
    # never be removed (reviewer finding, T-019)
    install = [p for p in sorted(REGISTRY.glob('*.REG'))
               if not EXCLUDE.search(p.name)]
    uninstall = sorted(REGISTRY.glob('*_REM.REG'))
    for p in install + uninstall:
        # map lookup: exact name, else the base install reg it removes
        mapping = reg_map.get(p.name) or reg_map.get(p.name.replace('_REM', ''), {})
        text, enc = read_reg(p)
        replaced = {}
        # longest label first: a label that is a substring of another
        # (e.g. future 'Сжать в MP4' vs 'Сжать в MP4 (HD)') must win
        for label, key in sorted(mapping.items(),
                                 key=lambda kv: len(kv[0]), reverse=True):
            if label not in text:
                report.append(f'  !! {p.name}: label missing: {label!r}')
                continue
            if key not in strings:
                report.append(f'  !! {p.name}: bundle key missing: {key}')
                continue
            n = text.count(label)
            text = text.replace(label, strings[key])
            replaced[label] = (n, strings[key])
        out = out_dir / p.name
        write_reg(out, text, enc)
        report.append(f'  {p.name}: {len(replaced)} labels '
                      f'({", ".join(f"{k} x{v[0]}" for k, v in replaced.items()) or "none"})')

    print('\n'.join(report))
    print(f'-> {out_dir} ({len(install) + len(uninstall)} files)')


if __name__ == '__main__':
    locale = sys.argv[1] if len(sys.argv) > 1 else 'et'
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else I18N / 'reg' / locale
    main(locale, out)
