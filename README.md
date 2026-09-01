# SAITULS

[Version 0.1.0](CHANGELOG.md#saituls-010-2026-09-01)

SAITULS adds utility commands to Windows File Explorer and bundles file, media
and Codex tools in one window. The installer fetches missing Python, FFmpeg,
yt-dlp, aria2 and Deno automatically.

## One-click install

1. Extract the whole SAITULS folder to a permanent location.
2. Double-click `INSTALL.cmd`.
3. Confirm the UAC prompt once.

The installer downloads only missing components, registers the context menu
entries and starts SAITULS. Re-run it any time to repair a moved or incomplete
installation.

Requires Windows 10/11 x64 and internet on first install without a pre-populated
`Bin` folder. Python, FFmpeg, yt-dlp, aria2 and Deno are installed automatically.
Codex CLI is needed only for LIMISAW.

Normal launch after install — `SAITULS.exe` or `SAITULS_LAUNCHER.cmd`. The
program runs without elevation; UAC only appears where Windows genuinely
requires it (e.g. menu installation).

## Main window

- **Home** shows what is missing and offers one-click install/repair.
- **Explorer menus** enables or removes selected Explorer commands. The whole
  row is a click target, not just the tiny checkbox.
- **Tools** launches the same utilities from one place, asking for the target
  file or folder first.
- **Settings** opens the program folder, fully exits SAITULS, and optionally
  enables silent tray autostart. Autostart is off by default.

The close button hides the window to the tray rather than terminating the
background controller. Use **Settings → Exit** or the tray menu **Exit** for
a full shutdown.

Keyboard: `Ctrl+1`…`Ctrl+4` switches tabs, `Tab`/`Shift+Tab` moves focus,
`Enter`/`Space` activates, `Esc` hides the window.

## 14 Explorer commands

| Command | What it does |
|---|---|
| Copy file path | Copies the full path of the selected file as text. |
| Delete duplicates | Finds identical files by size and SHA-256, shows a list, keeps the first copy and deletes the rest after confirmation. |
| Delete empty folders | Recursively removes empty directories after confirmation, logs to `Scripts\_.txt`. |
| Delete junk | Finds caches, logs, temp files and `build`/`dist`/`node_modules` directories; shows counts and first paths before deletion. |
| Delete same-name items | Flattens nested folders with the same name; conflicting names get a `_copy` suffix. |
| Download YouTube | Six modes: audio/video, with/without date, single or playlist. Links come from the clipboard. |
| FFmpeg actions | Conversion submenu for 23 media extensions. |
| Merge audio tracks | Merges two audio tracks inside the selected media file, preserving the video stream. |
| Repair MKV | Re-muxes MKV to MP4 or encodes to AV1. |
| Create project folders | Creates `_new_project` with `ae`, `c4d`, `_input`, `_output` subfolders. |
| Pack file/folder | Moves the item into a new adjacent folder `<name>_Packed`; adds a counter if the name already exists. |
| PowerShell as admin | Opens PowerShell as administrator in the selected folder. |
| Take ownership | Assigns the current user as owner of the selected item. |
| Show / hide files | Toggles hidden file visibility in Explorer. |

Deletion commands bypass the Recycle Bin. They always show confirmation, but
keeping a backup is still recommended.

## Built-in tools

The **Tools** tab contains:

- YouTube audio/video download — picks a destination folder first, then reads
  clipboard links;
- audio track merging;
- project skeleton creation;
- pack single file or whole folder;
- delete empty folders, duplicates, junk and nested same-name items;
- PowerShell as administrator;
- LIMISAW for Codex limits.

If a component is missing, SAITULS does not launch a broken command; it sends
the user to **Home → Install / repair**.

## LIMISAW — Codex limits in the tray

`LIMISAW.exe` reads the 5-hour and weekly remaining percentages of two local
Codex profiles: `%USERPROFILE%\.codex` and `%USERPROFILE%\.codex-account2`. It
calls the official `codex app-server` separately for each profile and does not
read `auth.json`, tokens or API keys.

The tray shows one large number rather than four digits crammed into 16 pixels.
The default metric is the lowest remaining percentage — the one that actually
matters. The **Tray number** menu can pin a specific account and period; the
tooltip explains the selected metric, while menu rows show both periods for
both accounts.

- drag the window by its top bar;
- `F5` refreshes, `Esc` or close hides the window;
- double-click the tray icon restores the window;
- data refreshes every 3 minutes;
- a real reset triggers a balloon notification;
- **Start with Windows** launches LIMISAW silently in the tray;
- a second launch activates the existing instance instead of creating another
  tray icon.

Python for the diagnostic probe is installed by `INSTALL.cmd`. The installer
deliberately does not set up Codex CLI or log into accounts — those are
personal credentials, not a dependency.

## Problip

`problip\Problip.exe` is a separate optional sound monitor. It beeps at a
configurable interval, keeps a list of watched processes and stores its settings
in `problip\problip.ini`. Problip is not a SAITULS tab and is not included in
the automatic install.

## OpenCode, Cline and Codex menus

The base install does not require OpenCode or Cline and does not fail if they
are absent. If both CLIs are already installed and Explorer YOLO entries are
wanted, run as administrator:

```powershell
powershell -ExecutionPolicy Bypass -File Installers\INSTALL_ALL.PS1 -IncludeAgentMenus
```

Separate installers:

- `Add-CodexContextMenus.ps1` — cascade menu for three isolated Codex accounts;
- `Remove-CodexContextMenus.ps1` — its removal;
- `Installers\INSTALL_AI_AGENT_MENUS.PS1 -Agent OpenCode` or `-Agent Cline` —
  installs one agent menu.

These commands do not install the CLIs themselves and do not create accounts.

## Removal and relocation

Select items in the **Explorer menus** tab and press **Remove**, or run:

```powershell
powershell -ExecutionPolicy Bypass -File Installers\INSTALL_ALL.PS1 -Uninstall
```

This removes Explorer entries but does not delete your files or the SAITULS
folder. Before relocating, close SAITULS and LIMISAW, move the entire folder,
then re-run `INSTALL.cmd` to update registry paths.

## What the installer downloads

All downloads come from the projects' own public sources or accepted Windows
build distributions:

- Python 3.13 x64 from `python.org` or via `winget`;
- FFmpeg Essentials from `gyan.dev`;
- standalone Windows binary `yt-dlp` from GitHub Releases;
- `aria2c` from the aria2 GitHub Releases;
- Deno x64 from the Deno GitHub Releases — needed by modern yt-dlp for
  JavaScript YouTube checks.

Re-running does not re-download existing heavy files. If the connection drops,
the installer shows the specific error, returns a non-zero exit code and does
not lie with a "success" message.

## For developers

Source files — `SAITULS.cs` and `LIMISAW.cs`; ready-to-run EXEs sit next to
them so a regular user does not need a compiler. Repository check:

```cmd
python tests\test_regs.py
```

Build on Windows with the built-in .NET Framework 4.x compiler:

```cmd
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -nologo -target:winexe -out:SAITULS.exe -optimize+ -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll SAITULS.cs
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -nologo -target:winexe -out:LIMISAW.exe -optimize+ -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll -r:System.Web.Extensions.dll LIMISAW.cs
```

The `i18n\strings` folder holds 33 string bundles. The ready-made registry
locale is currently included for Estonian; others are generated on demand via
`i18n\tools\gen_locale_reg.py`.

License — MIT, see `LICENSE`.