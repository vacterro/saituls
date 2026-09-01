# SAITULS

![Windows](https://img.shields.io/badge/Windows_10/11-x64-00ADEF?style=flat&logo=windows&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-blue?style=flat)
![Version](https://img.shields.io/badge/version-0.1.0-8A2BE2?style=flat)
![CI](https://img.shields.io/github/actions/workflow/status/vacterro/saituls/test.yml?style=flat&label=tests)
![C#](https://img.shields.io/badge/C%23-9B4993?style=flat&logo=csharp&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white)

**SAITULS** adds utility commands to Windows File Explorer and bundles file, media and Codex tools in one window. The installer fetches missing Python, FFmpeg, yt-dlp, aria2 and Deno automatically.

---

## ✨ Features

| | Category | What |
|---|---|---|
| 🖱️ | **14 Explorer commands** | Copy path, delete duplicates/empty/junk/same-name, YouTube download, FFmpeg conversion, MKV repair, project scaffolding, pack, PowerShell admin, take ownership, toggle hidden files |
| 📊 | **LIMISAW** | Codex rate-limit tray monitor — 5-hour and weekly remaining % for two accounts, reset detection, balloon notifications, lowest-remaining metric |
| 🔊 | **Problip** | Sound monitor (optional standalone) — configurable beep interval, 0–100 volume slider, process watchlist |
| 🧰 | **SAITULS GUI** | Home readiness, Explorer menu manager, Tools launcher, Settings — Golden Default Win95 UI, keyboard-navigable |
| 🤖 | **Agent menus** | OpenCode YOLO, Cline YOLO, Codex cascade (3 isolated accounts) |
| 🌐 | **i18n** | 33-locale string bundles, Estonian registry locale, per-locale reg generator |

---

## One-click install

```
1. Extract the whole SAITULS folder to a permanent location.
2. Double-click `INSTALL.cmd`.
3. Confirm the UAC prompt once.
```

The installer downloads only missing components, registers the context menu entries and starts SAITULS. **Re-run it any time** to repair a moved or incomplete installation.

Requires **Windows 10/11 x64** and internet on first install. Python, FFmpeg, yt-dlp, aria2 and Deno are installed automatically. Codex CLI is needed only for LIMISAW.

Normal launch after install — `SAITULS.exe` or `SAITULS_LAUNCHER.cmd`. The program runs **without elevation**; UAC only appears where Windows genuinely requires it (e.g. menu installation).

---

## Main window

| Tab | Purpose |
|---|---|
| **Home** | Shows what is missing and offers one-click install/repair |
| **Explorer menus** | Enables or removes selected Explorer commands — whole-row click targets |
| **Tools** | Launches utilities from one place, asks for target first |
| **Settings** | Opens program folder, full exit, optional silent tray autostart |

The close button hides the window to the tray rather than terminating the background controller. Use **Settings → Exit** or the tray menu **Exit** for a full shutdown.

Keyboard: `Ctrl+1`…`Ctrl+4` switches tabs, `Tab`/`Shift+Tab` moves focus, `Enter`/`Space` activates, `Esc` hides the window.

---

## 14 Explorer commands

| Command | Action |
|---|---|
| Copy file path | Copies the full path of the selected file as text |
| Delete duplicates | Finds identical files by size and SHA-256, lists, keeps first copy, deletes rest after confirmation |
| Delete empty folders | Recursively removes empty directories after confirmation, logs to `Scripts\_.txt` |
| Delete junk | Finds caches, logs, temp files, `build`/`dist`/`node_modules` directories; shows counts before deletion |
| Delete same-name items | Flattens nested folders with the same name; conflicting names get a `_copy` suffix |
| Download YouTube | Six modes: audio/video, with/without date, single or playlist. Links from clipboard |
| FFmpeg actions | Conversion submenu for 23 media extensions |
| Merge audio tracks | Merges two audio tracks inside the selected media file, preserving video stream |
| Repair MKV | Re-muxes MKV to MP4 or encodes to AV1 |
| Create project folders | Creates `_new_project` with `ae`, `c4d`, `_input`, `_output` subfolders |
| Pack file/folder | Moves the item into a new adjacent folder `<name>_Packed`; adds a counter if name exists |
| PowerShell as admin | Opens PowerShell as administrator in the selected folder |
| Take ownership | Assigns the current user as owner of the selected item |
| Show / hide files | Toggles hidden file visibility in Explorer |

Deletion commands **bypass the Recycle Bin**. They always show confirmation, but keeping a backup is still recommended.

---

## Built-in tools

The **Tools** tab contains:

- YouTube audio/video download — picks a destination folder first, then reads clipboard links
- Audio track merging
- Project skeleton creation
- Pack single file or whole folder
- Delete empty folders, duplicates, junk and nested same-name items
- PowerShell as administrator
- LIMISAW for Codex limits

If a component is missing, SAITULS does not launch a broken command; it sends the user to **Home → Install / repair**.

---

## LIMISAW — Codex limits in the tray

`LIMISAW.exe` reads the 5-hour and weekly remaining percentages of two local Codex profiles: `%USERPROFILE%\.codex` and `%USERPROFILE%\.codex-account2`. It calls the official `codex app-server` separately for each profile and does not read `auth.json`, tokens or API keys.

- **Tray**: one large number (lowest non-zero remaining % by default)
- **Menu**: pin a specific account and period, view both periods for both accounts
- **Reset detection**: compares previous vs current remaining %, triggers balloon notification on real reset
- **Relative times**: "in 3h 20m" instead of raw ISO strings
- `F5` refreshes, `Esc` hides, double-click tray restores window
- Data refreshes every 3 minutes
- **Start with Windows** launches silently in tray
- Second launch activates existing instance (no duplicate tray icons)

Python for the diagnostic probe is installed by `INSTALL.cmd`. The installer deliberately does not set up Codex CLI or log into accounts — those are personal credentials, not a dependency.

---

## Problip

`problip\Problip.exe` is a separate optional sound monitor. It beeps at a configurable interval with a volume slider (0–100), keeps a list of watched processes and stores its settings in `problip\problip.ini`. Problip is not a SAITULS tab and is not included in the automatic install.

---

## OpenCode, Cline and Codex menus

The base install does not require OpenCode or Cline and does not fail if they are absent. If both CLIs are already installed and Explorer YOLO entries are wanted, run as administrator:

```powershell
powershell -ExecutionPolicy Bypass -File Installers\INSTALL_ALL.PS1 -IncludeAgentMenus
```

Separate installers:
- `Add-CodexContextMenus.ps1` — cascade menu for three isolated Codex accounts
- `Remove-CodexContextMenus.ps1` — its removal
- `Installers\INSTALL_AI_AGENT_MENUS.PS1 -Agent OpenCode` or `-Agent Cline` — installs one agent menu

These commands do not install the CLIs themselves and do not create accounts.

---

## Removal and relocation

```powershell
powershell -ExecutionPolicy Bypass -File Installers\INSTALL_ALL.PS1 -Uninstall
```

This removes Explorer entries but does not delete your files or the SAITULS folder. Before relocating, close SAITULS and LIMISAW, move the entire folder, then re-run `INSTALL.cmd` to update registry paths.

---

## What the installer downloads

| Tool | Source |
|---|---|
| Python 3.13 x64 | `python.org` or `winget` |
| FFmpeg Essentials | `gyan.dev` |
| yt-dlp | GitHub Releases (standalone Windows binary) |
| aria2c | aria2 GitHub Releases |
| Deno x64 | Deno GitHub Releases |

Re-running does not re-download existing heavy files. If the connection drops, the installer shows the specific error, returns a non-zero exit code and does not fake a "success" message.

---

## For developers

Source files — `SAITULS.cs` and `LIMISAW.cs`; ready-to-run EXEs sit next to them so a regular user does not need a compiler. Repository check:

```cmd
python tests\test_regs.py
```

Build on Windows with the built-in .NET Framework 4.x compiler:

```cmd
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -nologo -target:winexe -out:SAITULS.exe -optimize+ -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll SAITULS.cs
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -nologo -target:winexe -out:LIMISAW.exe -optimize+ -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Web.Extensions.dll LIMISAW.cs
```

The `i18n\strings` folder holds 33 string bundles. The ready-made registry locale is currently included for Estonian; others are generated on demand via `i18n\tools\gen_locale_reg.py`.

**License** — MIT, see `LICENSE`.

---

## Repository structure

```
SAITULS/
├── SAITULS.cs / SAITULS.exe     # Main control-panel (WinForms)
├── LIMISAW.cs / LIMISAW.exe     # Codex limit tray monitor (WinForms)
├── problip/Problip.cs/.exe      # Sound monitor (WinForms, standalone)
├── Registry/                     # 14 context-menu .reg files + REM counterparts
├── Scripts/                      # Batch, Python, PowerShell workers
├── Installers/                   # INSTALL_ALL.PS1, agent-menu installers
├── i18n/                         # 33-locale strings, docs, registry locale
│   ├── strings/                  # 33 × 69-key translation bundles
│   ├── docs/                     # 33-locale documentation
│   └── reg/et/                   # Estonian registry locale
├── Wiki/                         # 4-page project wiki
├── tests/test_regs.py            # Integrity suite
├── INSTALL.cmd / setup.ps1      # One-click installer
├── CHANGELOG.md                  # Release history
└── LICENSE                       # MIT
```

Heavy binaries (ffmpeg ~153 MB ×2, ffprobe, yt-dlp, AV1/RTX40 builds, Ghostscript, ExifCleaner) are not committed. Download the payload zip from the latest release and extract into `Bin\` / `Bin\App\` — or run `BUILD_PAYLOAD.cmd` against a full local tree.