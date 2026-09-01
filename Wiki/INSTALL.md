# Install / uninstall

## Recommended one-click install

Double-click `INSTALL.cmd` at the repository root. It self-elevates once,
downloads only missing Python/media tools, installs the 14 Explorer features,
and starts SAITULS without an administrator token. Re-run the same file to
repair a moved or incomplete installation.

## Full install (all features)

```powershell
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_ALL.PS1
```

What it does:

1. Self-elevates (reg import into `HKLM`/`HKCR` needs admin).
2. Imports every `Registry\*.REG` **except** `*_REM*`, `*_UTF16*`,
   `*_REWRITTEN`, and `FFMPEG.REG` — 14 files.
   `FFMPEG_REM.REG` stays live so previously-installed legacy FFmpeg keys
   can still be removed.
3. Each is applied via `reg.exe import` (hidden window, sequential),
   with `%%ROOT%%` substituted for the real toolkit root at import time
   (T-034) — the install set is relocation-proof: the whole folder can be
   moved/copied to another drive or path and reinstalled without editing
   the `.reg` files.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_ALL.PS1 -Uninstall
```

Imports every `Registry\*_REM*.reg` (removal counterparts). Complete
coverage of the FFmpeg menu is provided by `FFMPEG_REM.REG` (removes all
keys `FFMPEG_MENU.REG` installs, including the 7 legacy extensions).

## GUI install or selective changes

```cmd
SAITULS_LAUNCHER.cmd
```

The launcher starts `SAITULS.exe` as the current user. Use **Home → Install /
repair** for the complete setup, or **Explorer menus** to check individual
features and click **Install** or **Remove**. Only the registry write requests
UAC.

## OpenCode and Cline YOLO menus

OpenCode/Cline are optional and are not part of the default install. If both
CLIs are already available, add their Explorer entries with:

```powershell
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_ALL.PS1 -IncludeAgentMenus
```

The optional entries cover folder icons, folder backgrounds, and drive roots:

- `OpenCode YOLO here` runs `opencode <project> --auto`.
- `Cline YOLO here` runs `cline --cwd <project> --auto-approve true --tui`.

Install or verify only this pair without touching the other menu features:

```powershell
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_AI_AGENT_MENUS.PS1
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_AI_AGENT_MENUS.PS1 -VerifyOnly
```

Both entries use `Scripts\AI_AGENT_LAUNCHER.PS1`. It keeps the project name
at the start of the console title, reapplies that title if a TUI overwrites
it, and uses the shared Wintage Golden console profile. The installer enables
the modern console edit keys: with a selection, `Ctrl+C` copies; without a
selection it still reaches the agent as cancel; `Ctrl+V` pastes. It also keeps
`Ctrl+Shift+C/V` enabled as a fallback. This intentionally modifies the user's
Windows console profile; uninstall preserves that profile.

Remove only these entries with `Remove-AgentContextMenus.ps1`. The Wintage
palette and console keyboard preferences are intentionally preserved.

## Per-file import

> **WARNING (T-037):** never import the path-bearing `.reg` files directly
> with `reg import`. They are `%%ROOT%%`-tokenized (T-034) — a direct import
> writes literal `%%ROOT%%\Bin\...` commands into the registry and the menu
> entries end up broken. Use the safe wrapper:

```powershell
powershell -ExecutionPolicy Bypass -File .\Registry\IMPORT_SAFE.PS1 -File .\Registry\FFMPEG_MENU.REG
powershell -ExecutionPolicy Bypass -File .\Registry\IMPORT_SAFE.PS1 -File .\Registry\FFMPEG_MENU_REM.REG
```

`IMPORT_SAFE.PS1` substitutes `%%ROOT%%` with the toolkit root (or the `-Root`
you pass), writes a temp copy and imports that. Removal files (`*_REM.REG`)
have no `%%ROOT%%` tokens, but running them through the wrapper is harmless
and keeps the habit uniform.
