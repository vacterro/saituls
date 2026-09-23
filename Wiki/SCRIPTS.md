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
- **Settings** — toolkit root display, INI path, app autostart toggle, the
  **Taskbar** section (`Reliable taskbar edge reveal` on/off plus
  `Start with SAITULS / Windows` and a live helper status line),
  `Open folder`, `Exit`.

## Reliable taskbar edge reveal (`Scripts/taskbar_edge/`)

`TaskbarEdge.exe` is a standalone non-elevated per-user background helper. With
taskbar auto-hide on, pushing the pointer into a monitor's bottom edge reveals
that monitor's taskbar even when another window owns or covers the activation
pixels.

It samples the real cursor position (`GetCursorPos` + `MonitorFromPoint`, Per-
Monitor DPI Awareness V2) every 30 ms, requires two consecutive samples in the
last 2 px of the monitor's full rectangle, resolves that monitor's bar through
`SHAppBarMessage(ABM_GETAUTOHIDEBAREX, ABE_BOTTOM)` — falling back to
`Shell_TrayWnd` / `Shell_SecondaryTrayWnd` mapped by `MonitorFromWindow` — and
refreshes its topmost z-order with a single `SetWindowPos` that never moves,
resizes, activates or focuses anything. Explorer keeps owning the hide.

One instance per interactive session (`Local\SaitulsTaskbarEdge`). SAITULS may
only start it, stop it, show its status and toggle its autostart; no edge
detection lives in `SAITULS.cs`. Its lifetime is independent of the SAITULS
window — closing to the tray or exiting SAITULS leaves it running, and the
Settings switch is the explicit stop.

```
TaskbarEdge.exe --status     non-sensitive desktop and helper facts
TaskbarEdge.exe --self-test  shell discovery and reveal invariants
TaskbarEdge.exe --stop       ask the running instance to exit
```

Advanced values live in `Scripts\taskbar_edge\taskbar_edge.ini`
(`edge_pixels`, `poll_ms`, `dwell_ms`, `cooldown_ms`, `reveal_strategy`,
`debug`); bad values fall back to defaults rather than stopping the helper. Full
detail in `Scripts/taskbar_edge/README.md`.

## `Bin/App/YOUTUBE.INI` — legacy GUI command source

The legacy `__ContextMenu+.exe` reads its YouTube buttons from this file
(relative to `Bin\App\`). It runs the commands verbatim, so `yt-dlp` and
`ffmpeg` must resolve on PATH; `SAITULS_LAUNCHER.cmd` (and the original
`SAITULS_LAUNCHER.cmd`) prepends `Bin\` and `Bin\App\` so they do regardless
of the system PATH. `SAITULS.exe` does not depend on this file.

- `[YoutubeVideo]` → `yt-dlp -f "bestvideo+bestaudio/best"` → `%USERPROFILE%\Videos\SAITULS`
- `[YoutubeAudio]` → `yt-dlp -f bestaudio` → `%USERPROFILE%\Music\SAITULS`, re-encodes to MP3 via `ffmpeg`

## Legacy GUI internals — `Bin/__CONTEXTMENU+.EXE` (reverse-engineered, no source)

`__ContextMenu+.exe` is a compiled AutoHotkey app ("GUI Shell FFmpeg" by
Satirov, 2023). The `.ahk` source was **not found** on the machine
(searched the original script archive and related tool directories),
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

## Secure Apps (`Scripts/secure_apps/`)

FIDO2-gated launcher for applications whose data should not be readable when
the application is closed. Obsidian is the reference profile; the design is
profile-driven, so further protected applications are added through
`secure_apps.json` rather than new code.

| file | role |
|---|---|
| `SECURE_APPS.ps1` | launcher + `-SelfTest` probe; owns no security logic |
| `secure_apps.pyw` | thin launcher for `secure_apps_gui.py`; owns no logic |
| `secure_apps_gui.py` | Golden Default window, hosts the single-instance broker |
| `secure_apps.json` | versioned, fail-closed profile registry |
| `sa_broker.py` | sessions, policy, idle, system events, secure-lock sequence |
| `sa_auth.py` | `ISecureAuthProvider`: YubiKey hmac-secret, external helper, fake |
| `sa_storage.py` | `ISecureStorageBackend`: BitLocker VHDX, fake |
| `sa_crypto.py` | HKDF-SHA256 -> AES-256-GCM key hierarchy, zeroizing buffers |
| `sa_applife.py` | protected process tree, graceful close, protected activity |
| `sa_state.py` | durable non-secret state + pessimistic crash reconciliation |
| `sa_audit.py` | allowlist audit log |
| `sa_enroll.py` | enrollment, one-time recovery material, extra keys |
| `sa_migrate.py` | plaintext -> encrypted migration with a resumable journal |
| `sa_privhelper.py` + `sa_storage_helper.ps1` | elevated storage channel over a named pipe |
| `sa_cli.py` | headless surface: `selftest`, `status`, `enroll`, `open`, `lock`, `mode`, `recover`, `migrate`, `audit` |

Default mode separates the authentication session lifetime (six idle hours)
from the vault mounted lifetime (only while the application runs). Migration
never deletes the plaintext copy; it reports `MIGRATION_VERIFIED` **and**
`PLAINTEXT_SOURCE_REMAINS` until the user removes it. Full contract:
`Scripts/secure_apps/README.md`.

## AI consoles (`Scripts/consoles/`)

Data-driven, NON-elevated launcher for interactive agent CLIs:
`claude-1` and `claude-2` (distinct `CLAUDE_CONFIG_DIR`, shared `claude`
command) and `antigravity` (`agy`). The registry stores a bare command name
and an argument list; nothing in it can become a shell command. The existing
Explorer-menu OpenCode/Cline launcher keeps its own maximum-privilege
behaviour and is untouched. Contract: `Scripts/consoles/README.md`.

## Shell Doctor (`Scripts/shell_doctor/`)

Answers "why does Explorer / the Start button hang for seconds" from evidence
instead of folklore. Tools tab → **Shell Doctor**, or
`powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\shell_doctor\SHELL_DOCTOR.ps1`.

| File | Role |
|------|------|
| `SHELL_DOCTOR.ps1` | collector (event logs, disks, shell-extension registry, loaded DLLs, Bags, caches) + console report/menu |
| `shell_doctor_logic.ps1` | pure findings engine over a snapshot hashtable; no I/O, fixture-tested |

What it ranks, worst first:

- **Storage faults** — System log `Event 129` resets and `153` retries (any
  storage driver: `UASPStor`, `USBSTOR`, `storahci`, `stornvme`, …) plus `7`/`51`/`157`,
  per `\Device\RaidPortN`, mapped to the physical disk. A *regular* cadence
  (e.g. every ~285 s) points at a USB bridge/enclosure dropping out, and every
  reset stalls I/O long enough to freeze Explorer, Start and Everything. Fix is
  hardware: rear motherboard port, no hub, bridge firmware, or another enclosure.
- **Shell crashes/hangs** — Application log `Event 1000` (crash) / `1002` (hang) for `explorer.exe`.
- **Idle shell extensions** — context-menu/overlay/thumbnail DLLs of sync clients
  (MEGA, Yandex.Disk, Google Drive, Adobe CoreSync, …) loaded into Explorer
  while the app itself is not running; dangling registrations whose DLL is gone.
- **Thumbnail-handler overlap** — two providers (e.g. SageThumbs + Icaros)
  claiming the same extensions.
- **View-state bloat** — thousands of `Bags` entries.

Flags: `-NoPause` (report only), `-Json`, `-Days N` (default 7). The only
mutation is `-ResetViews` (or the menu item): per-user `Bags`/`BagMRU` and
thumbnail/icon caches, `.reg` backup first, then Explorer restart. It never
writes HKLM, services, power plans or device settings — those findings come
with advice, not an action. Tests: `tests/test_shell_doctor.ps1`.
