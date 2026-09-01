# SAITULS UI contract

SAITULS and LIMISAW use the Golden Default palette and compact Windows 95
geometry defined by the bound SAIPEN `UI.md`: Verdana, non-antialiased GDI
text, square corners, flat fills, two-pixel bevels and no animation.

Both windows are borderless but draggable from their top title strip. The
right-hand `X` hides the window in the tray; an explicit tray or Settings
command exits the process. Everyday launch is not elevated. Operations that
modify protected registry areas request UAC only when invoked.

## SAITULS

The 560x440 window stays inside a 640x480 logical desktop and has four tabs:

- **Home** is the primary screen. It reports readiness for Python, FFmpeg,
  yt-dlp, aria2, Deno and LIMISAW, then offers one `Install / repair` action.
- **Explorer menus** exposes the 14 installable menu features with whole-row
  checkbox hit targets and separate Install/Remove actions.
- **Tools** launches bundled workers. File/folder operations always ask for a
  target; YouTube actions also ask for a destination folder.
- **Settings** shows real paths, optional quiet autostart, open-folder and
  explicit exit actions.

Autostart is off by default. When enabled, SAITULS starts with `--minimized`
and does not steal focus.

Keyboard operation is complete: `Ctrl+1` through `Ctrl+4` select tabs,
`Tab`/`Shift+Tab` move the dotted focus rectangle, `Enter`/`Space` activate
the focused item, and `Esc` hides the window.

## LIMISAW

The tray icon contains exactly one readable percentage. `lowest` is the
default metric; the tray menu can pin account 1/account 2 and 5-hour/weekly.
The selected metric is named in the tooltip, while disabled status rows in
the tray menu show both periods for both accounts.

The window has one global `Refresh now` action, fixed account panels, and a
persistent status/error footer. `F5` refreshes, `Esc` hides, double-clicking
the tray icon opens the existing instance. A second process activates the
first instead of adding another tray icon. Window position is saved.

## Personal hotkeys

The local ignored `___AHK/___MAIN.ahk` keeps `Ctrl+Shift+Delete` for Process
Explorer. It does not bind `Ctrl+Delete` to Task Manager. Windows' native
`Ctrl+Shift+Esc` Task Manager shortcut remains unchanged.
