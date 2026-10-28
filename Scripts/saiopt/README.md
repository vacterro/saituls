# SAIOPT — background optimizer

SAIOPT runs quietly in the tray and keeps the machine responsive while it is
busy. One rule sits behind everything it does:

> **What you are using or hearing keeps full speed. Everything else yields —
> but only while the machine is actually short of CPU, only by one priority
> step, and only for as long as that stays true.**

Everything it changes, it changes back. It never "cleans RAM", never deletes a
file and never touches services, HKLM or Explorer.

## The three engines

| Engine | Runs | What it does |
| --- | --- | --- |
| **Ease** | automatically | While the machine is at `busy_pct`+ CPU, a background process that has used `hog_core_pct`+ of one core for `hog_samples` samples in a row drops from **Normal** to **BelowNormal**. It goes back to Normal the moment you switch to it, it starts playing sound, it quiets down, or the machine has headroom again. |
| **Watch** | automatically, read-only | Storage resets (Event 129) named by the real drive letter, **lost writes** (NTFS 50/140, disk 51/157), how fast the taskbar answers, and Explorer's GDI/USER count. Reported as words, not coloured dots. |
| **Keep awake** | automatically, read-only | A USB disk whose bridge resets it on a fixed idle timer gets one uncached 4 KiB read every `keep_awake_sec`. |
| **Sweep** | asks first | Stale orphans: a process whose parent is gone, with no window, that has been frozen (every thread suspended) for `stale_suspended_min`, or a console tool that has not used one CPU tick for `stale_idle_hours`. **End** asks with the PIDs listed. **Auto** arms one image name, after a confirmation. |

### Ease: who is never touched

- the app you are using, its whole process family (a browser's renderers
  included), and for `focus_grace_ms` after you leave it;
- anything making sound (`protect_audio=1`);
- the shell and system: Explorer, DWM, csrss, the console hosts, audiodg,
  the Start/Search hosts, Task Manager, Process Explorer, AutoHotkey (a slowed
  input hook lags the whole mouse), OBS, and SAIOPT/SAITULS/TaskbarEdge;
- the service session (session 0) and other users;
- any process **not at Normal** — someone set that on purpose (a governor
  like `GOV.ps1` with its ffmpeg, you, the app itself), and it stays theirs;
- a process whose priority someone else changes after SAIOPT eased it: it is
  forgotten, never fought, never touched again.

`never_ease` adds exe names to that list.

### Why BelowNormal is safe

BelowNormal only matters when every core is contended. With spare cores the
process runs exactly as fast as before; when the machine is saturated, Normal
work — the window you are in, the taskbar, Start — gets the CPU first. It is
the same idea as Process Lasso's ProBalance, without the parts that hurt.

### What SAIOPT deliberately does not do

- **No RAM "cleaning".** Trimming working sets or purging the standby list
  makes the next second slower: every page comes back as a hard fault.
- **No file deletion.** `DEL_JUNK` owns junk files, behind its own gates.
- **No automatic kills out of the box.** `sweep_auto` is empty until you arm
  an image. SAISPIN, not SAIOPT, handles spinning runaways.
- **No services, no HKLM, no Explorer restart, no elevation.**

## Watch: disks

Event 129 names `\Device\RaidPort<N>`, and `N` stops matching the SCSI port
after a USB re-plug. SAIOPT follows the real `\\.\Scsi<port>:` link, reads
letters from `MSFT_Partition`, and remembers disks across runs
(`disks.txt`), so a disk that drops off is still named:

```
U: Samsung SSD 970 EVO (USB) - gone          reset earlier
  2 in the last hour, 161 in 24 h, every 4m45s, last 22:37
```

A burst of failed writes is one incident, told once, with the volumes named:

```
E: S:                                      WRITES LOST at 23:05
  5 lost writes, 6 journal flush failures, 13 paging errors
  Run chkdsk /f when it is back; check files written then.
```

## Keep awake

Some USB bridges (the JMicron JMS583 in the incident enclosure) put an idle
drive to sleep on a firmware timer of about five minutes and then fail to wake
it inside the storport timeout. The result is an Event 129 reset every ~285 s,
and each one stalls Explorer and Start for seconds. Measured on the incident
machine:

| Window | Reads | Resets |
| --- | --- | --- |
| idle, before | none | every 4m45s, like clockwork |
| 23:50 - 00:05 | one 4 KiB read every 45 s | **0** |

So a disk that shows exactly that signature is kept busy with a tiny read:
USB bus, at least four resets, a periodic cadence, and present now. The read is
`CreateFile(GENERIC_READ, OPEN_EXISTING, FILE_FLAG_NO_BUFFERING)` of a random
4 KiB block of an existing non-system file on that volume. Nothing is ever
written, and it runs on its own thread, so a disk stuck mid-reset cannot stall
the engine.

A disk earns this once and keeps it. It is remembered by letters and model in
`keepawake.txt`, not by its RaidPort number, which changes on every re-plug.
Once the resets stop they eventually age out of the 24 h window, and the cure
does not switch itself off when that happens. The row's `Keep awake` button
turns it off for that disk, and `keep_awake=off` turns it off everywhere. The
panel says `KEPT AWAKE` only while it is actually holding (no reset since it
started, no failing read); otherwise it says so in red.

This is a workaround, not the fix. The fix is bridge firmware without the
standby timer (JMicron tools can set it to 0), a different enclosure, or a
port the bridge is happy on.

## Panel and tray

Left-click the tray icon (three bars) for the panel; right-click for Pause /
Resume / Exit. The panel is Golden Default: Verdana without antialiasing,
square corners, two-pixel bevels, no animation, status as a word —
`CALM`, `EASING`, `ATTENTION`, `PAUSED` — with the reason beside it.
`Esc` hides it; `Tab`/`Enter` work.

SAITULS → **Tools → Optimizer** starts SAIOPT, or reaches the running copy,
and opens the panel. `Start with Windows` writes
`HKCU\...\Run\SaitulsSaiopt`; it is off until you turn it on.

## Command line

```
SAIOPT.exe              run quietly in the tray
SAIOPT.exe --panel      run (or reach the running copy) and open the panel
SAIOPT.exe --status     report state
SAIOPT.exe --self-test  prove sampling, priority round trip, audio, disks, shell probe
SAIOPT.exe --stop       stop the running copy (it restores every eased process)
SAIOPT.exe --restore    restore what a crashed copy left eased (only when not running)
SAIOPT.exe --shot FILE  render the panel from live, read-only data to a PNG
```

## `saiopt.ini`

Written with defaults next to the exe on first run; edits apply within 5 s.
Out-of-range values fall back to the default and are logged as a warning.

| Key | Default | Range |
| --- | --- | --- |
| `ease` | `1` | `0`/`1` |
| `busy_pct` | `60` | 20–100 |
| `calm_pct` | `35` | 5–95, below `busy_pct` |
| `hog_core_pct` | `50` | 10–6400 (% of one core) |
| `hog_samples` | `3` | 1–60 |
| `calm_samples` | `10` | 1–300 |
| `sample_ms` | `2000` | 500–10000 |
| `ease_to` | `below_normal` | `below_normal`, `idle` |
| `max_eased` | `16` | 1–64 |
| `focus_grace_ms` | `15000` | 0–600000 |
| `protect_audio` | `1` | `0`/`1` |
| `never_ease` | empty | exe names |
| `watch_storage` | `1` | `0`/`1` |
| `keep_awake` | `auto` | `auto`, `off` |
| `keep_awake_sec` | `45` | 15–240 |
| `watch_shell` | `1` | `0`/`1` |
| `shell_slow_ms` | `250` | 20–5000 |
| `stale_suspended_min` | `60` | 5–10080 |
| `stale_idle_hours` | `12` | 1–720 |
| `sweep_auto` | empty | exe names |
| `tray`, `balloons` | `1` | `0`/`1` |
| `remind_min` | `60` | 5–1440 |

## Files

| File | Purpose |
| --- | --- |
| `OptLogic.cs` | Pure rules: config, guards, Ease, Sweep, Watch models, words. No Win32, no I/O, no clock. |
| `SAIOPT.cs` | Win32 adapter, engine loop, tray, panel, command line. |
| `SelfTest.cs` | `--self-test`. The only process it changes or ends is a suspended child it starts itself. |
| `build.ps1` | `csc` x64 build to `SAIOPT.exe`. |
| `saiopt.ini` | Settings (not tracked). |

State lives in `%LOCALAPPDATA%\SAITULS\saiopt\`: `status.txt`, `eased.txt`
(what is eased right now — a crashed copy's successor adopts and restores it),
`disks.txt` (last known disks), `keepawake.txt` (disks kept awake or declined),
`seen.txt` (incidents you acknowledged) and `saiopt.log` (capped at 512 KiB).

Cost: one `NtQuerySystemInformation` call per sample returns every process
with its CPU time, priority, parent and thread states — no handle is opened to
look at anybody. The audio meter is only read while it can matter.

## Tests

```
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_saiopt_logic.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_saiopt_contract.ps1
Scripts\saiopt\SAIOPT.exe --self-test
```

The first two are hermetic and run in CI. The self-test needs a real desktop.
