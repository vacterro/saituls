#!/usr/bin/env python
"""Add the menu.open_in key to every locale bundle.

The bundle contract in i18n/INDEX.md is "every locale carries the same key
set", so a new menu key lands in all 33 files or the contract is broken.
Idempotent, and order- and line-ending-preserving: the bundles are hand-edited
too, and a whole-file reformat would bury the one line that actually changed.
"""
import io
import json
import pathlib
import sys

VALUES = {
    "ar": "فتح في",
    "bg": "Отвори в",
    "cs": "Otevřít v",
    "da": "Åbn i",
    "de": "Öffnen in",
    "ded": "Открыть в",
    "el": "Άνοιγμα σε",
    "en": "Open in",
    "es": "Abrir en",
    "et": "Ava rakenduses",
    "fi": "Avaa sovelluksessa",
    "fr": "Ouvrir dans",
    "he": "פתח ב",
    "hi": "यहाँ खोलें",
    "hr": "Otvori u",
    "hu": "Megnyitás itt",
    "id": "Buka di",
    "it": "Apri in",
    "ja": "開く",
    "ko": "열기",
    "nl": "Openen in",
    "no": "Åpne i",
    "pl": "Otwórz w",
    "pt": "Abrir em",
    "ro": "Deschide cu",
    "ru": "Открыть в",
    "sk": "Otvoriť v",
    "sv": "Öppna med",
    "th": "เปิดด้วย",
    "tr": "Aç",
    "uk": "Відкрити в",
    "vi": "Mở bằng",
    "zh": "打开",
}

KEY = "menu.open_in"
root = pathlib.Path(__file__).resolve().parents[1] / "strings"
changed, missing = [], []
for loc, value in sorted(VALUES.items()):
    path = root / ("strings.%s.json" % loc)
    if not path.is_file():
        missing.append(loc)
        continue
    with io.open(str(path), "r", encoding="utf-8", newline="") as handle:
        raw = handle.read()
    doc = json.loads(raw)
    if doc.get(KEY) == value:
        continue
    if KEY in doc:
        print("%s already has %s with a different value; edit it by hand" % (loc, KEY))
        sys.exit(1)
    eol = "\r\n" if "\r\n" in raw else "\n"
    # splice one line in after the opening brace, key order untouched
    brace = raw.index("{") + 1
    tail = raw[brace:]
    tail = tail.lstrip("\r\n")
    line = eol + "  %s: %s," % (json.dumps(KEY), json.dumps(value, ensure_ascii=False)) + eol
    with io.open(str(path), "w", encoding="utf-8", newline="") as handle:
        handle.write(raw[:brace] + line + tail)
    changed.append(loc)

print("updated: %d" % len(changed))
print("missing bundle files: %s" % (", ".join(missing) or "none"))
sys.exit(1 if missing else 0)
