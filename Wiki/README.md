# SAITULS — Unified Windows Toolkit

One self-contained toolkit that installs and manages Windows Explorer
context-menu extensions, runs a silent "blip" monitor, and launches the
bundled mini-tools. Everything is driven by `SAITULS.exe`, a Golden Default
(Wintage) GUI compiled from `SAITULS.cs`.

Project root: `<SAITULS-ROOT>`

## What it provides

- **SAITULS.exe unified GUI** (C# / WinForms, Golden Default per saipen
  `UI.md`) — four tabs: **Menus** (checkbox-driven context-menu
  install/remove), **Monitor** (blip volume/interval/autostart), **Tools**
  (mini-tool launchers), **Settings** (root path, autostart, exit).
- **File and folder utilities** — copy path as text, take ownership,
  PowerShell as administrator, toggle hidden files, delete empty folders,
  delete duplicate files, delete nested same-name files, pack a file or
  folder into a `_Packed` folder.
- **Media tools** — YouTube download (via `yt-dlp`), audio-track merging
  (`ffmpeg`), MKV re-mux to MP4 / re-encode to AV1, and a generic
  ffmpeg conversion submenu covering 23 file extensions.
- **Project scaffolding** — creates a `_new_project` folder skeleton
  (`ae`, `c4d`, `_output`, `_input`).
- **AI agent launchers** — OpenCode and Cline YOLO entries for folders,
  backgrounds, and drives, with a project-first guarded title, Wintage Golden
  console palette/skill access, and enabled Ctrl+C/Ctrl+V console shortcuts.
- **Codex CLI cascaded menu** — `Add-CodexContextMenus.ps1` installs a
  3-account cascaded submenu under `Directory` + `Directory\Background`
  (parent verb `Открыть Codex здесь` with three child verbs, one per
  isolated Codex account; `SubCommands=""` pattern is the only one that
  renders the submenu reliably on this box — see E-072). Run elevated
  once; `Remove-CodexContextMenus.ps1` is the uninstall counterpart.
- **Problip monitor** — optional persistent "meditation beeper" tray app
  (C# source + compiled exe under `problip/`), separate from SAITULS; the
  SAITULS **Monitor** tab drives the same blip engine embedded in
  `SAITULS.exe`.

## Quick start

```cmd
SAITULS_LAUNCHER.cmd
```

The launcher self-elevates to Administrator and starts `SAITULS.exe`. Use
the **Menus** tab to install or remove context-menu features; the **Monitor**
tab to control the blip; the **Tools** tab to launch bundled workers.

## Testing

`tests/test_regs.py` is the declared integrity suite (`saipen test` / `tt`):
validates install-set reg parse + encoding (14 files), resolves every
referenced path, checks 33-locale key parity and `reg-map.json` completeness,
parses the AI agent PowerShell scripts, and self-tests both YOLO command
lines. Run with:

```
python tests\test_regs.py
```

Exit 0 = PASS. Add a check here when you change the reg/i18n tree.

## Layout

| Path | Purpose |
|---|---|
| `SAITULS.cs` / `SAITULS.exe` | Unified GUI source + compiled binary |
| `SAITULS_LAUNCHER.cmd` | Elevating launcher for `SAITULS.exe` |
| `Registry/` | `.reg` files: one per menu feature (14), plus `*_REM.REG` uninstall counterparts |
| `Scripts/` | `.CMD` and `.PYW` (Python) workers the menu entries invoke |
| `Bin/` | Bundled binaries: `FFMPEG.EXE`, `yt-dlp.exe`, `ARIA2C.EXE`, `FFPROBE.EXE`, `LAUNCHER.EXE`; batch wrappers `FFMPEG_RUN.BAT`, `AV1_COMPRESS.BAT` |
| `Bin/App/` | Legacy `__ContextMenu+.exe` payload (ffmpeg, yt-dlp, `YOUTUBE.INI`, `AV1 CPU\`, `RTX40__\`, `GS\`, `ExifCleaner\`) |
| `Installers/` | PowerShell install/uninstall drivers: `INSTALL_ALL.PS1`, `INSTALL_GUI.PS1`, `INSTALL_AI_AGENT_MENUS.PS1` |
| `problip/` | Standalone blip monitor (C# source + exe + ini) |
| `_FREEBUFF_MENU/` | Freebuff-specific context menu entries (separate from the toolkit) |
| `Wiki/` | This documentation set |

> **No git repository** — this folder is not version-controlled. The SAIPEN
> overlay under `.saipen/` tracks the work instead (`mode: no-publish`).

## Maintenance state

- `Registry/FFMPEG.REG` was **permanently removed** on 2026-08-07
  (T-020/T-021) — a legacy hybrid that deleted whole
  `SystemFileAssociations` keys. `FFMPEG_MENU.REG` covers all 23
  extensions instead.
- `Bin/App/` (payload for the legacy `__ContextMenu+.exe`) was restored
  from `_TRASH_20260807/` on 2026-08-07 (12 entries) — the GUI's own `App\`
  folder, holding `yt-dlp.exe`, `ffmpeg.exe`, `YOUTUBE.INI` and the
  preset-tool subfolders `AV1 CPU\`, `RTX40__\`, `GS\` (Ghostscript for
  PDF), `ExifCleaner\`. The legacy GUI checks for these at launch; missing
  ones produce a "ffmpeg not found" warning, so they are a runtime
  dependency of that launcher, not dead weight.
> **Note (T-027):** four binaries exist as byte-identical pairs in `Bin/`
> and `Bin/App/` (`FFMPEG.EXE`, `FFPROBE.EXE`, `yt-dlp.exe`, `ARIA2C.EXE`,
> ~252MB per side). This is intentional but heavy: the legacy GUI
> (`__ContextMenu+.exe`) resolves `App\...` relative to its working
> directory, while `Scripts/` and the reg-embedded `Bin\FFMPEG_RUN.BAT`
> wrapper reference `Bin\`. Consolidation was rejected — it would require
> rewriting ~90 `FFMPEG_MENU.REG` command paths and the legacy GUI's
> hardcoded `App\` layout for 252MB on a 1.6TB drive. Keep both; update
> both when bumping a binary.

> **Note (T-030):** `.freebuff/` is the **live SQLite database** of
> `freebuff.exe` (3 instances running as of 2026-08-07), not
> project content. `desktop-v2.db` (+`-wal`/`-shm`) receives active writes.
> Do not move, copy into backups, or delete it while Freebuff runs; if you
> want it elsewhere, quit Freebuff first and let the app pick a new home.

- `i18n/` holds the 33-locale string bundle, `reg-map.json` (label → key
  mapping), and `tools/gen_locale_reg.py`. `INSTALL_ALL.PS1 -Lang <locale>`
  installs localized menu labels; all 33 locales are pre-generated under
  `i18n/reg/<locale>/` with full 14-feature install-set parity.
  DL_YT.REG's 7 labels (main "Скачать с YouTube" + 6 submenu) are
  mapped in `reg-map.json` (T-032): 6 keys
  `dl_yt.sub_*` exist in all 33 locales, so `-Lang <locale>`
  localizes the download menu too.
- The toolkit moved from `<OLD-ROOT>`
  to `<SAITULS-ROOT>` on 2026-08-31 (T-055) and was
  rebranded from the "Windows Context Menu Customization Toolkit" to
  **SAITULS**.
