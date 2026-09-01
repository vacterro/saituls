# SAITULS 0.1.1 (2026-09-01)

## What changed

- README and Wiki are now fully **English**; Cyrillic menu/UI labels are
  documented in English (actual `.reg` files keep their UTF-16 Cyrillic labels
  by design). Added GitHub community files: issue/PR templates, SECURITY.md,
  CONTRIBUTING.md; repo description and topics set; README gained badges,
  a feature matrix and a repository map.
- LIMISAW tray icon is rendered **non-antialiased at native 16×16** (was 32×32
  downscaled by the shell → blur). App icon is the `heh` avatar
  (`heh.ico`), embedded via `/win32icon:`. Program icon restored.
- LIMISAW `lowest` tray metric now shows the **minimum non-zero** remaining
  percentage (skips exhausted 5h windows, falls back to 0 only when all are 0).
- LIMISAW **quiet autostart**: `AutoStart=1`, registry Run key, launches
  `--minimized` into the tray.
- Problip gained a **0–100 volume slider** (replaced preset buttons); drag,
  click-to-jump, value saved to `problip.ini`.
- Codex account launchers hardened: `Start-Codex-Main.ps1` now explicitly sets
  `CODEX_HOME=$HOME\.codex` so inherited environment cannot open account 2/3
  under the main account. All three isolated launchers verified against their
  own profiles.
- AI_AGENT_LAUNCHER.PS1 `Get-StablePort` switched from FNV-1a (uint64 overflow)
  to a SHA-256-based deterministic port.
- `tests/test_regs.py` README contract updated to the English tokens.

# SAITULS 0.1.0 (2026-09-01)

- Added `INSTALL.cmd` and turned `setup.ps1` into a non-interactive,
  repeatable install/repair flow. Missing Python, FFmpeg, yt-dlp, aria2 and
  Deno are provisioned automatically; optional OpenCode/Cline menus no longer
  break the core installation when their CLIs are absent.
- Reorganized SAITULS around a Home readiness screen, clearer Explorer-menu
  names, full-row checkbox hit targets, safe dependency checks, destination
  selection for YouTube downloads, and non-elevated everyday launch.
- LIMISAW now has reliable title-bar dragging, one readable tray percentage
  (lowest remaining by default), a single refresh action, shortcut keys,
  double-click restore, single-instance behavior and quiet autostart.
- Destructive cleanup scripts now explain their scope and ask before deleting.
- Removed the personal `Ctrl+Delete` Task Manager hotkey; the native Windows
  `Ctrl+Shift+Esc` shortcut remains untouched.
- Rewrote README against the shipped behavior and current dependency model.

# SAITULS 0.0.2 (2026-09-01)

## What changed

- `SAITULS.exe` is now a pure launcher/installer hub. The embedded blip
  Monitor tab and its engine were removed; Problip stays in the repo as a
  standalone tool (`problip/Problip.exe`), launched from the Tools tab.
  Tabs: Menus | Tools | Settings.
- `LIMISAW.exe` is now tray-first: it draws the live limit numbers directly
  in the tray icon. The tray menu lets you pick which values to show per
  account — C1 5h / C1 weekly / C2 5h / C2 weekly, any combination. Added a
  reset detector (compares previous vs current remaining % and reset time to
  confirm an OpenAI-side reset) and quiet balloon notifications on reset.
  Reset times are shown as friendly relative values ("in 3h 20m") instead of
  raw ISO strings, and the accent palette (green/yellow/red) is brighter for
  readability on the dark surface.
- Problip autostart verified: per-user Run key points at the relocated exe,
  `AutoStart=1` in `problip.ini`, starts silently to tray.

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
