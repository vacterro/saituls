# SAITULS UI contract

SAITULS uses the local Golden Default / Wintage UI contract from `saipen/UI.md`.
The main window is a compact tabbed dialog: Verdana, classic bevelled controls,
no rounded corners, shadows, gradients, animation or transparency.

All client text uses GDI `CreateFontW` with `NONANTIALIASED_QUALITY=3` and is
assigned directly with `WM_SETFONT`. Pixel QA must show only foreground and
background colours in glyph crops; intermediate AA colours are forbidden.

The distributable is `SAITULS.exe`, compiled from `SAITULS.cs` with
`csc.exe` (.NET Framework 4.8). Its window icon is the multi-resolution
`SAITULS.ico`, derived from `SAIPEN_OrangeShine.png`.

## Palette

Only the Golden Default tokens are used:

- backgroundSoft `#232018`
- surfaceRaised `#3D372A`
- textPrimary `#D4C89A`
- borderHighlight `#F0D060`

## Behavior

- **Menus tab** shows 14 context-menu features. The tray button is functional
  but the recommended workflow is the tabbed panel.
- **Monitor tab** has the same Lite/Micro/Full mode trio as the original
  `problip` contract. `Lite` is the default. The blip WAV is scaled before
  playback, so the setting works without changing the system volume or
  installing an audio DLL. `Super silent` enforces a 1.4-second minimum gap.
- **Tools tab** launches the bundled scripts and binaries. It does not check
  for `App\` payloads — that is the legacy `__ContextMenu+.exe`'s concern.
- **Settings tab** shows the toolkit root, INI path, and app autostart.
- Windows autostart is enabled by default through the per-user `Run` key.
  The controller immediately starts both the tray and monitor when it starts.
- Settings are saved in `SAITULS.ini` beside `SAITULS.exe` and are applied
  by restarting only the monitor. Process Explorer is not restarted during a
  sound/mode change.

## Controls

- Tray `Open SAITULS` opens the compact editor.
- Tray `Monitor ON` / `Monitor OFF` start/stop the blip.
- `Ctrl+Delete` starts Process Explorer and the monitor together, hidden.
  (Requires the legacy `Problip.exe` — not yet implemented in SAITULS.exe.)
- Holding left `Ctrl+Alt` alone for 350 ms opens Process Explorer visibly.
  The delay keeps the quick `Ctrl+Alt+Delete` chord available for Task Manager.
- `Ctrl+Alt+Delete` requests Task Manager. Windows may consume the secure
  attention sequence before AutoHotkey; `Ctrl+Shift+Esc` remains the native
  guaranteed shortcut, and the tray button always launches `taskmgr.exe`.