# TaskbarEdge — reliable bottom-edge taskbar reveal

When Windows taskbar auto-hide is on, pushing the mouse pointer into the bottom
edge of a monitor should reveal that monitor's taskbar. Sometimes it does not:
another window owns or covers the activation pixels, and Explorer's own trigger
loses the race.

TaskbarEdge is a small non-elevated background helper that watches the *real*
cursor position and, when the pointer is genuinely pinned against a monitor's
bottom edge, asks the shell to bring that monitor's auto-hide bar back to the
front. It supplements Windows auto-hide; it never replaces it.

## Files

| File | Purpose |
| --- | --- |
| `EdgeLogic.cs` | Pure geometry, config and state machine. No Win32, no I/O, no clock. |
| `TaskbarEdge.cs` | Win32 adapter, single-instance process, diagnostics. |
| `build.ps1` | `csc` build to `TaskbarEdge.exe`. |
| `taskbar_edge.ini` | Advanced values, written with defaults on first run. Not tracked. |

Build:

```
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\taskbar_edge\build.ps1
```

## Running it

SAITULS owns the switches — **Settings → Taskbar**:

- `Reliable taskbar edge reveal` starts or stops the helper and remembers the
  choice in `SAITULS.ini` (`TaskbarEdge=1`). It is **off** until you turn it on
  once; upgrading SAITULS never enables it.
- `Start with SAITULS / Windows` writes the per-user Run entry
  `HKCU\...\CurrentVersion\Run\SaitulsTaskbarEdge`.

Lifetime is deliberately independent of the SAITULS window. Hiding SAITULS to
the tray leaves the helper running, and so does a full SAITULS exit. The one
explicit stop is the Settings switch (or `TaskbarEdge.exe --stop`).

## Command line

```
TaskbarEdge.exe              start the per-session watcher
TaskbarEdge.exe --status     report desktop and helper state
TaskbarEdge.exe --self-test  verify shell discovery and reveal invariants
TaskbarEdge.exe --stop       ask the running instance to exit
```

Only one instance may run per interactive session; the named mutex
`Local\SaitulsTaskbarEdge` enforces it and a second launch exits `0`.

`--status` reports non-sensitive desktop facts only: running state, auto-hide
state, monitor count, current monitor rectangle, whether the cursor is in the
edge strip, taskbar handle validity and discovery method, last reveal method
and result, and the Explorer/taskbar generation. The cursor's coordinates are
never printed.

## How it works

1. **Per-Monitor DPI Awareness V2** is applied before any geometry is read, so
   `GetCursorPos` and the monitor rectangles share one physical coordinate
   space. Older shells fall back to per-monitor, then system awareness.
2. Every `poll_ms` the helper reads `GetCursorPos` and resolves the monitor
   under that point with `MonitorFromPoint`. It never uses `WindowFromPoint`,
   the foreground window, desktop ownership or `WM_MOUSEMOVE` delivery —
   depending on any of those would recreate the bug.
3. The activation strip is the last `edge_pixels` rows of that monitor's
   **full** rectangle, not its work area. All coordinates are signed, so
   monitors placed left of or above the primary display work exactly the same.
4. A reveal needs **two consecutive in-strip samples** *and* the `dwell_ms`
   window. At the 30 ms / 30 ms defaults that is the second sample — immediate
   to a human, but enough that sliding the pointer across the seam between
   vertically stacked monitors does not fire.
5. The bar for that monitor is found with the documented
   `SHAppBarMessage(ABM_GETAUTOHIDEBAREX, ABE_BOTTOM)` against that monitor's
   rectangle. If the documented lookup returns nothing, the helper falls back to
   enumerating `Shell_TrayWnd` and `Shell_SecondaryTrayWnd` and mapping each
   window back with `MonitorFromWindow`. A bar is only ever accepted for the
   monitor it actually sits on.
6. Auto-hide is confirmed through `SHAppBarMessage(ABM_GETSTATE)` before any
   reveal. `StuckRects3` is never read or written.
7. Debounce: after one reveal for a monitor the helper goes quiet until the
   pointer leaves the strip and returns, the bar hides itself again after
   `cooldown_ms`, or the taskbar identity changes.

Off the edge the loop is three cheap cursor/monitor calls; the appbar and window
queries only happen while the pointer is actually in the strip. Measured idle
cost on the target machine was **0.0 % CPU over 10 s, ~14 MB working set**.

## Reveal strategy

The shipped strategy — `reveal_strategy=topmost` — is one call:

```
SetWindowPos(bar, HWND_TOPMOST, 0, 0, 0, 0,
             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_FRAMECHANGED)
```

It refreshes the bar's topmost z-order so a normal or topmost desktop window
cannot visually suppress the reveal. It does not move the bar, resize it,
activate it or call `SetForegroundWindow`, and Explorer keeps owning the hide
animation completely.

Measured on the target Windows 10 shell (build 19045, three monitors, one at
negative X):

| Property | Result |
| --- | --- |
| `SetWindowPos` returns | `TRUE` |
| Bar z-order | raised, index 21 → 17 |
| Bar position/size | unchanged |
| Foreground HWND | unchanged |
| Bar after cursor leaves | hides natively, no pinning |
| Reveals with the helper running | 20/20 on each of three monitors |

A narrow secondary strategy, `reveal_strategy=topmost_mousemove`, adds one
shell-directed `WM_MOUSEMOVE` posted to the bar after the same `SetWindowPos`.
It moves no cursor and injects no input. It is **not** enabled by default and is
only worth trying if the primary strategy is ever observed to miss: the helper
never runs several invasive strategies per sample.

## `taskbar_edge.ini`

Written with defaults on first run. Out-of-range, unparsable or missing values
fall back to that key's default and are recorded as a warning — a bad config
never stops the helper.

| Key | Default | Range |
| --- | --- | --- |
| `edge_pixels` | `2` | 1–32 |
| `poll_ms` | `30` | 10–250 |
| `dwell_ms` | `30` | 0–500 |
| `cooldown_ms` | `150` | 0–5000 |
| `reveal_strategy` | `topmost` | `topmost`, `topmost_mousemove` |
| `debug` | `0` | `0`, `1` |

`debug=1` records transitions only — `EDGE_ENTER`, `REVEAL_REQUEST`,
`REVEAL_OK`, `EDGE_EXIT`, `TASKBAR_REDISCOVERED` — to a log capped at 256 KiB in
`%LOCALAPPDATA%\SAITULS\taskbar_edge\`. There is no verbose polling log.

## Explorer restarts

Taskbar handles are never permanently cached. Every sample revalidates with
`IsWindow`, the resolution is re-derived at least every two seconds, and the
Explorer process behind `Shell_TrayWnd` is part of the generation value. When
Explorer restarts, the generation changes, debounce state is dropped and the new
bars are picked up automatically — no SAITULS restart, no helper restart.

## Scope

Bottom edge only, in this wave. Exclusive DirectX fullscreen is best-effort: no
attempt is made to force the taskbar over a true exclusive-fullscreen
application.

## Tests

```
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_taskbar_edge_logic.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_taskbar_edge_contract.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_taskbar_edge_integration.ps1
```

The first two are hermetic and run in CI. The integration harness needs a real
interactive desktop; add `-AllowCursorMove` for the 20-of-20 acceptance run,
`-AllowExplorerRestart` for the recovery case, and `-AllowShellStateChange` if
auto-hide is off (it restores the previous state in `finally`).
