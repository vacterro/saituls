# SAITULS 0.0.1 (2026-08-31)

First public release. The toolkit was consolidated from a scattered personal
setup into one repository and one GUI.

## What ships

- `SAITULS.exe` — Golden Default (Wintage) WinForms GUI compiled from
  `SAITULS.cs`: Menus / Monitor / Tools / Settings tabs.
- `LIMISAW.exe` — Codex rate-limit monitor (`LIMISAW.cs`): probes account 1
  (`~\.codex`) and account 2 (`~\.codex-account2`) via the codex app-server
  JSON-RPC protocol and shows the 5-hour and weekly remaining percentages
  with reset times. Read-only; never parses `auth.json` or touches tokens.
  Reachable from SAITULS's Tools tab (`Codex Limits`).
- 14 Explorer context-menu features (`Registry/`, installed via
  `Installers/INSTALL_ALL.PS1` or the GUI's Menus tab).
- OpenCode / Cline YOLO launchers, Codex cascaded menu.
- Problip standalone tray monitor (`problip/Problip.cs` -> `Problip.exe`).
- 33-locale string bundle + per-locale reg generation
  (`i18n/tools/gen_locale_reg.py`).
- Integrity suite `tests/test_regs.py`.

## Source-only repo

Heavy binaries (ffmpeg ~153 MB x2, ffprobe, yt-dlp, AV1/RTX40 builds,
Ghostscript, ExifCleaner) are NOT committed. Download
`SAITULS-payload-0.0.1.zip` from the release assets and extract into
`Bin\` / `Bin\App\` at the toolkit root — or run `BUILD_PAYLOAD.cmd`
against a full local tree.

## Known notes

- Context-menu commands are `%%ROOT%%`-tokenized; use
  `Registry/IMPORT_SAFE.PS1` (or `INSTALL_ALL.PS1`) to import — a raw
  `reg import` writes literal `%%ROOT%%` paths and breaks the menu.
- `___AHK/` (personal autostart scripts) and `.saipen/` (project memory)
  are intentionally absent from the public repository.
