# Scripts reference

Workers the context-menu entries invoke. `.CMD` files are batch; `.PYW`
files are Python windowed (no console) using `tkinter` for dialogs.

## `Scripts/`

| File | Function |
|---|---|
| `DL_YT.CMD` | YouTube downloader. Puts `..\Bin` first on PATH so yt-dlp finds `aria2c`, uses bundled Deno for current YouTube checks, and reads clipboard URLs. The first argument selects `audio`, `video`, `audiodated`, `videodated`, `audioplaylist`, or `videoplaylist`; the optional second argument is the destination folder. Format: AV01≤1080 preferred, merges to MKV. Without a destination it uses `%cd%`, with a safe Downloads fallback for `system32` or the script dir. |
| `MERGE_AUD.CMD` | `ffmpeg -filter_complex "[0:a]amerge=inputs=2[aout]"` — merges two audio tracks, replaces the original on success; on error deletes partial output and pauses |
| `NEW_PROJ.CMD` | Creates `_new_project\ae`, `_new_project\c4d`, `_new_project\_output`, `_new_project\_input` under the target folder |
| `PACK.PYW` | Packs the selected file/folder into `base_Packed` (counter suffix if exists), tkinter UI |
| `DEL_DUP.PYW` | Finds duplicates by partial/full SHA-256, previews redundant copies, and deletes only after confirmation |
| `DEL_EMPTY.PYW` | Confirms, then walks bottom-up and removes empty directories; failures are logged instead of crashing |
| `DEL_SAME.PYW` | Confirms, then flattens nested same-name folders (lowercase, spaces/`-` → `_`); conflicts gain `_copy` |
| `DEL_JUNK.PYW` | Self-contained junk rules; previews counts/paths and warns that caches, logs, `build`, `dist`, and `node_modules` are deleted without Recycle Bin |
| `AI_AGENT_LAUNCHER.PS1` | Shared OpenCode/Cline launcher: explicit YOLO arguments, literal project CWD, project-first guarded title, UTF-8 and Wintage console host |
| `..\SAITULS.exe` | Unified GUI: `SAITULS.cs` compiled to `SAITULS.exe` (Golden Default WinForms) |
| `Legacy/` | Old scripts, not part of the active install set |

## AI agent launcher

`AI_AGENT_LAUNCHER.PS1 -Agent OpenCode|Cline -WorkDir <folder>` resolves the
installed `.cmd` shim before launch and fails visibly if the CLI or project is
missing. OpenCode receives `--auto`; Cline receives `--auto-approve true
--tui`. **Self-elevates to Administrator** (UAC) when the host is not already
elevated and `-SelfTest` is not set — maximum-privilege launch by default. A
native title guard restores `<project> | <agent> YOLO | <path>` every 250 ms
so an agent's terminal-title escape sequence cannot permanently replace the
project heading. On agent failure a "Press Enter to close" prompt keeps the
window visible for diagnostics. `-SelfTest` prints the resolved command, exact
arguments, title check, and global Vintage-skill check without starting a TUI.

## `Bin/` batch wrappers

| File | Function |
|---|---|
| `FFMPEG_RUN.BAT` | Generic ffmpeg wrapper: `input`, `args`, `suffix_ext`. Runs `FFMPEG.EXE -i input args output`; on `%ERRORLEVEL% NEQ 0` prints error and pauses |
| `AV1_COMPRESS.BAT` | SVT-AV1 encode: `-c:v libsvtav1 -preset 6 -crf 24 -pix_fmt yuv420p10le -svtav1-params tune=0`, output `<name>_AV1<ext>`; pauses on error |

## SAITULS GUI

`SAITULS.exe` is the unified control panel. Tabs:

- **Home** — live dependency readiness and one `Install / repair` action.
- **Explorer menus** — checkbox grid of the 14 context-menu features, `Check All`
  / `Uncheck All`, `Install` / `Remove`. Self-elevates by relaunching
  `INSTALL_ALL.PS1` (or `IMPORT_SAFE.PS1` as fallback).
- **Tools** — launches the bundled scripts and binaries with PATH
  prepended to `Bin\` and `Bin\App\`; every file/folder action first asks
  for its target.
- **Settings** — toolkit root display, INI path, app autostart toggle,
  `Open folder`, `Exit`.

## `problip` (standalone monitor)

`problip/` holds the C# source + compiled `Problip.exe` for the older
standalone tray monitor. It is **not** the same binary as `SAITULS.exe`;
it is kept for compatibility with the previous `problip.ini` settings and
autostart key. SAITULS has no embedded blip engine; Problip is fully separate
and remains available only for users who want that sound monitor.

Run the compiled `Problip.exe` (or `Problip.cs` recompiled with csc). It
registers itself in the per-user Windows `Run` key as `Problip` and starts
the blip engine hidden. Persistent mode blips at a fixed interval
(5 / 10 / 15 seconds, configurable via `PersistentIntervalMs`) regardless
of whether Process Explorer or Task Manager is running. Super-silent mode
rate-limits blips to one per 1.4 seconds while the baseline continues
refreshing. The compact tray panel persists mode, volume (presets
0.01 / 0.1 / 0.33 / 0.5), persistent interval presets (5 / 10 / 15 s),
silent, autostart and the custom-process list in `problip.ini`.

Client text is rendered with GDI non-antialiased quality; the panel is a
fixed-size Golden Default surface.

## `Bin/App/YOUTUBE.INI` — legacy GUI command source

The legacy `__ContextMenu+.exe` reads its YouTube buttons from this file
(relative to `Bin\App\`). It runs the commands verbatim, so `yt-dlp` and
`ffmpeg` must resolve on PATH; `SAITULS_LAUNCHER.cmd` (and the original
`SAITULS_LAUNCHER.cmd`) prepends `Bin\` and `Bin\App\` so they do regardless
of the system PATH. `SAITULS.exe` does not depend on this file.

- `[YoutubeVideo]` → `yt-dlp -f "bestvideo+bestaudio/best"` → `P:\__STORE_P\_YT_VIDEO`
- `[YoutubeAudio]` → `yt-dlp -f bestaudio` → `V:\___VAC\_MUS\_YT_MUSIC`, re-encodes to MP3 via `ffmpeg`

## Legacy GUI internals — `Bin/__CONTEXTMENU+.EXE` (reverse-engineered, no source)

`__ContextMenu+.exe` is a compiled AutoHotkey app ("GUI Shell FFmpeg" by
Satirov, 2023). The `.ahk` source was **not found** on the machine
(searched `V:\___VAC\__K\__CODE\___AHK\` + its `_ARCHIVE\`, plus a
tree-wide grep for `Satirov`/`RTX40`/`ExifCleaner`/`GUI Shell FFmpeg`),
so this map is observed behavior, not source. Read it before changing the
GUI's payload folders — it is the only record of what it checks.

**Launch-time file checks** (static text, read via window probes):
- needs `App\ffmpeg.exe` or `App\AV1 CPU\ffmpeg.exe` → else warning
  "ffmpeg.exe or AV1 CPU needed on disk"
- needs `App\AV1 CPU\ffmpeg.exe` → else warning
  "AV1 CPU ffmpeg.exe not found"
- when all present the status static reads "File status: (ok)"

**Button → tool wiring (observed):**

| Button | Uses |
|---|---|
| Compress MP4 (CPU) | `App\AV1 CPU\FFMPEG.EXE` (SVT-AV1 CPU preset) |
| RTX | `App\RTX40__\FFMPEG.EXE` (NVIDIA NVENC) |
| Compress PDF | `App\GS\` (Ghostscript: `GSDLL64.DLL`, `GSWIN64C.EXE`) |
| ExifCleaner | `App\ExifCleaner\EXIFCLEANER.EXE` |
| YouTube / Download files | `App\YOUTUBE.INI` commands (bare `yt-dlp` on PATH) + `App\AUDIO.EXE` / `App\VIDEO.EXE` helpers |
| Replace MP3 / Extract WAV / Make GIF / Split frames / MKV / Flac to AAC | ffmpeg via `App\ffmpeg.exe` |

**Layout rule (why `Bin/App/` exists):** the legacy GUI resolves
`App\...` relative to its working directory, and `SAITULS_LAUNCHER.cmd` sets
CWD to `Bin\` — so the GUI's payload must live at `Bin\App\`. Deleting or
renaming any of the four tool folders re-triggers the launch warnings
(T-025/T-026 restore history). `SAITULS.exe` does not require this
folder; the legacy `__ContextMenu+.exe` does.
