# SAITULS UI contract

SAITULS uses the Golden Default palette and compact Windows 95 geometry defined
by the bound SAIPEN `UI.md`: Verdana, non-antialiased GDI text, square corners,
flat fills, two-pixel bevels and no animation.

The window is borderless but draggable from its top title strip. The right-hand
`X` hides it in the tray; an explicit tray or Settings command exits the
process. Everyday launch is not elevated. Operations that modify protected
registry areas request UAC only when invoked.

## SAITULS

The 560x440 window stays inside a 640x480 logical desktop and has four tabs:

- **Home** is the primary screen. It reports readiness for Python, FFmpeg,
  yt-dlp, aria2 and Deno, then offers one `Install / repair` action.
- **Explorer menus** exposes the 14 installable menu features with whole-row
  checkbox hit targets and separate Install/Remove actions.
- **Tools** launches bundled workers. File/folder operations always ask for a
  target; YouTube actions also ask for a destination folder.
- **Settings** shows real paths, optional quiet autostart, the `Taskbar`
  section, open-folder and explicit exit actions.

The `Taskbar` section carries two switches and one status word:
`Reliable taskbar edge reveal` (off by default; an upgrade never turns it on)
and `Start with SAITULS / Windows`, with the helper reported as `running`,
`stopped`, `enabled, not running` or `helper missing`. Polling cadence, dwell
and cooldown are deliberately not in the GUI — they live in
`Scripts\taskbar_edge\taskbar_edge.ini`.

`TaskbarEdge.exe` itself has no window at all: no tray icon, no dialog, no
message loop. Its lifetime is independent of this window — closing SAITULS to
the tray or exiting it outright leaves the helper running, and the Settings
switch is the one explicit stop.

Autostart is off by default. When enabled, SAITULS starts with `--minimized`
and does not steal focus.

Keyboard operation is complete: `Ctrl+1` through `Ctrl+4` select tabs,
`Tab`/`Shift+Tab` move the dotted focus rectangle, `Enter`/`Space` activate
the focused item, and `Esc` hides the window.

## Scenarios

The dedicated scenario launcher (`Scripts\scenarios\SCENARIOS.ps1`) follows
the same Golden Default geometry in a 520x430 fixed dialog. It is opened by
the single `Scenarios` button in the SAITULS Tools tab, never elevated
itself.

- Category dropdown (`All`, `Clean`, `Image`, `Audio`, `Video`, `Backup`) and
  an instant search box filter one list of scenarios; each row shows the
  label and its first safety badge, disabled entries are marked
  `[disabled]`.
- The selected pane shows the description, target mode, dependency summary
  and for blocked scenarios the exact blocker. The badge line uses the
  warning color, the danger color for destructive scenarios.
- `Run` resolves the scenario from the data registry, asks for a file or
  folder target when the entry declares one, and shows an explicit OK/Cancel
  confirmation for every destructive action (`This action modifies or deletes
  files.`). Elevation (`runas`) happens only for entries marked
  `requires_admin`, per action.
- `Open folder` opens the scenario's own directory in Explorer; `Close` and
  `Esc` close the window.
- Keyboard complete via standard WinForms tab order; `Esc` closes. No
  animations, no scrollbars, no web UI.

## Secure Apps

The Secure Apps window (`Scripts\secure_apps\secure_apps_gui.py`, PyQt6;
`secure_apps.pyw` next to it is a thin launcher that only imports and runs
that module's `main` -- there is exactly one implementation)
follows the same Golden Default contract as the Queue Viewer: Verdana at
11px, non-antialiased, square corners, two-pixel bevels, no animation, zero
white. It is opened by the single `Secure Apps` button in the SAITULS Tools
tab and is never elevated; the only elevated component is the short-lived
storage helper, which raises one UAC prompt when a vault is first attached.

- One row per profile, initially `Obsidian`, with columns `Profile`,
  `State`, `Vault`, `Session`, `Last activity`, `Idle timeout`.
- The detail panel restates `Session status`, `Vault status`,
  `Last activity`, `Idle timeout`, `Mount path` and one `Message` line.
- Actions: `Open`, `Lock now`, `Mode: Default`, `Mode: Aggressive`,
  `Recover`, `Manage key`, `Manage profile`. The selected mode is the
  pressed-bevel button; `Lock now` uses the danger colour and confirms
  before it closes anything.
- **Status is always a word.** The ten states -- `LOCKED`, `AUTH_REQUIRED`,
  `AUTHENTICATING`, `UNLOCKING`, `MOUNTED`, `RUNNING`, `SESSION_CACHED`,
  `LOCKING`, `RECOVERY_REQUIRED`, `ERROR` -- are rendered verbatim with a
  short explanation beside them. There is no coloured dot that means
  "probably fine": a green light is not an acceptable way to tell somebody
  whether their vault is readable right now.
- `Manage key` reports the connected authenticator's capability in words and
  refuses enrollment when the CTAP2 `hmac-secret` extension is absent.
- `Manage profile` is read-only. The registry is the authority and is
  validated fail-closed on load.
- The recovery dialog shows the BitLocker material once and requires the
  phrase `I HAVE STORED IT` to be typed. It offers no way to save the value.
- Nothing security-related runs on the GUI thread: the broker lives on a
  worker thread behind queued signals, because a frozen window during a
  security prompt is how people learn to click things they should not.

## AI consoles

The console picker (`Scripts\consoles\CONSOLES.ps1`) is a 460x300 fixed
WinForms dialog in the same Golden Default geometry as the Scenarios
launcher, opened by the single `AI Consoles` button in the Tools tab.

- One list of profiles (`Claude 1`, `Claude 2`, `Antigravity CLI`); an
  unresolvable command is marked `[command missing]` in the row rather than
  failing at launch time.
- The detail pane names the command, its resolved path, the child
  environment keys and the elevation level, which is always `none`.
- `Open console` asks for a project folder and opens a fresh, NON-elevated
  console titled `<project> | <profile> | <path>`. `Esc` and `Close` close
  the picker.
- Ctrl+C and Ctrl+V keep working in the opened console, and a nonzero exit
  keeps the window open instead of vanishing.

## SAISPIN

SAISPIN keeps process sampling and policy evaluation in its headless watcher.
Its compact 430x400 Settings window is available from the SAITULS Tools tab.
The editor follows Golden Default: dark brown surfaces, square controls,
two-pixel bevels, Verdana text and the palette tokens above. Reading or editing
the per-user policy does not request elevation.

- The Detection tab sets the CPU threshold, required hot samples, minimum hot
  duration, sample window, task cadence, stale-history window and notification
  reminder. The Process rules tab edits the never-kill allowlist and per-image
  rules. Process rules can tighten thresholds or force alert-only; they cannot
  weaken hard system or identity guards.
- **Dry-run is the default**, and it is what a fresh installation registers.
  Moving an unarmed task to Auto-kill requires an explicit confirmation.
  Saving unrelated settings cannot arm it.
- Test Sweep samples current processes and reports sampled, suspicious and
  alert counts. It always runs in dry-run, writes no watchdog state, sends no
  notification and never terminates a process.
- Settings are validated as a complete policy before save. The config is saved
  before the existing transactional task installer runs. If re-registration
  fails, the prior config is restored and the prior task remains in place.
- **An alert names the process, the number and the mode** — PID, image, CPU
  share of one core, how long it has been hot, whether it is an orphan, and
  whether the run was dry. A command line is trimmed to ~200 characters for the
  balloon; the full text goes to the log.
- **Nothing is terminated on one observation.** Defaults require six
  consecutive hot samples spanning at least half an hour, an orphaned parent,
  a re-verified identity and a non-allowlisted image. Every process still needs
  the hard system, parent and history checks.
- **The same news is told once.** A sweep runs every five minutes and a runaway
  can spin for hours, so a repeat balloon for a process already reported is
  suppressed for the configured reminder interval. A changed verdict — an
  alert that became a kill, a refusal that became a real termination — is new
  information and always shows immediately. Several processes caught in one
  sweep share one balloon that names the count, never one balloon each.
  Suppression touches the balloon only; the log keeps every sweep.

## Shell limits the window respects

The borderless title strip decodes the hit-test point from a pair of *signed*
16-bit coordinates, so dragging works on monitors positioned above or to the
left of the primary, not only on the primary itself.

A tray tooltip is capped at 63 characters, because that is where the shell
starts rejecting it — and rejecting it aborts the icon update too, leaving a
stale picture in the tray.

## Personal hotkeys

The local ignored `___AHK/___MAIN.ahk` keeps `Ctrl+Shift+Delete` for Process
Explorer. It does not bind `Ctrl+Delete` to Task Manager. Windows' native
`Ctrl+Shift+Esc` Task Manager shortcut remains unchanged.
