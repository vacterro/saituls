# Scripts reference

The SAITULS shell owns only launcher selection, target choice and result display.
Workers own behavior. The `.CMD` files are batch; `.PYW` files are Python
windowed; subsystem entry points preserve their own privilege and recovery
contracts.

## Context-menu workers

| File | Function |
|---|---|
| `DL_YT.CMD` / `DL_YT.PS1` | YouTube download. `DL_YT.PS1` is the GUI worker; both resolve `Bin\`, bundled Deno, yt-dlp and aria2. The first path argument selects `audio`, `video`, `audiodated`, `videodated`, `audioplaylist` or `videoplaylist`; the second is the destination folder. |
| `MERGE_AUD.CMD` / `MERGE_AUD.PS1` | Require exactly two audio streams, preserve video, subtitles, data and attachments by stream copy, write a unique scratch output, validate it with ffprobe, then atomically replace the source only on success. |
| `NEW_PROJ.CMD` | Creates `_new_project\ae`, `_new_project\c4d`, `_new_project\_output` and `_new_project\_input`. |
| `PACK.PYW` | Packs the selected file/folder into `base_Packed`, adding a counter suffix on collision. |
| `DEL_DUP.PYW` | Previews partial/full SHA-256 duplicates, then deletes only confirmed redundant copies. Final disposition is handle-based; no pathname unlink fallback. |
| `DEL_EMPTY.PYW` | Confirms, walks bottom-up, and removes empty directories. Failures are logged, not swallowed. |
| `DEL_SAME.PYW` | Confines flattening to the selected root, refuses reparse points, uses same-volume rename, and uses collision-safe names. |
| `DEL_JUNK.PYW` | Uses `SAFE_DISK` for drive roots and `AGGRESSIVE_PROJECT` only for an explicitly selected project folder. `--all-disks` never reaches the aggressive policy. |
| `AI_AGENT_LAUNCHER.PS1` | OpenCode/Cline Explorer-menu launcher. It is intentionally maximum-privilege, project-CWD literal, title-guarded and stable-port aware; `-SelfTest` is non-elevated. |

`Scripts/DESTRUCTIVE-SAFETY.md` is the normative contract for the deletion
workers. `DEL DUP` uses Windows file-handle disposition on the verified
candidate. `DEL SAME` refuses reparse boundaries. `DEL EMPTY` removes only
empty directories. `DEL JUNK --all-disks` journals durable `INTENT` before
each candidate, records terminal outcomes after it, and reports
`UNKNOWN_AFTER_INTENT` after a crash rather than inventing a deletion. Exit
codes are `0` complete/no-op, `1` cancelled before mutation, `2` fatal/partial
destructive run, and `3` safety gate blocked.

## Shared worker and payload helpers

| File | Function |
|---|---|
| `shell_arg.py` | One Explorer argument normalizer. `%1` can arrive as a drive-relative `G:` when a drive-root quote is malformed; every worker takes its target through `shell_target`. |
| `verified_file.py` | Handle-based Windows deletion helper. The kept and candidate files remain open without write/delete sharing through final digest and disposition. |
| `del_junk_policy.py`, `DEL_JUNK_RULES.py` | Separate `SAFE_DISK` and `AGGRESSIVE_PROJECT` rules; repository metadata and audit evidence remain protected from deletion. |
| `del_junk_walk.py` | Iterative, depth-independent tree traversal and removal shared by safety paths. |
| `del_junk_disks.py` | Drive eligibility via Windows drive-type APIs; every excluded root gets a reason. |
| `del_junk_errors.py` | Failure taxonomy from exception type and `errno`/`winerror`, never localized message text. |
| `media_payload.ps1` | Validates staged media files (size, PE sanity and optional version probe) before publication; publishes coupled ffmpeg/ffprobe members as one generation with rollback. |
| `build_payload.ps1` | Builds the release ZIP from `PAYLOAD_MANIFEST.txt`, rejects missing/partial members and publishes only after validation. |
| `restore_payload.ps1` | Restores a configured payload backup; `-VerifyOnly` reports without writing. |
| `item_tools.py` | Shared Item List / Item Tree engine: deterministic Unicode output, recursion-safe traversal, exclusion rules and atomic publication. |

## `Bin/` media runtime

| File | Function |
|---|---|
| `Bin/FFMPEG_RUN.BAT` | Thin compatibility launcher. It passes the complete FFmpeg argument string through `SAIFMPEG_ARGS`; it does not own conversion safety. |
| `Bin/FFMPEG_RUN.PS1` | Shared runner behind `FFMPEG_MENU.REG` and `MKV_FIX.REG`. Resolves encoder capability before work, runs FFmpeg with `-nostdin`, writes to a GUID scratch path, probes output, and publishes atomically. It prints `SAIFMPEG_STATUS=<code>:<detail>` and exits nonzero on refusal: `0` success, `2` input missing, `3` FFmpeg missing, `4` FFprobe missing, `5` encoder unavailable (no silent CPU fallback), `6` output collision, `7` FFmpeg failure, `8` validation failure, `9` publication failure, `10` bad usage. |
| `Bin/AV1_COMPRESS.BAT` | Legacy SVT-AV1 wrapper: preset 6, CRF 24, 10-bit yuv420p, output `<name>_AV1<ext>`. The current Explorer AV1 action uses `FFMPEG_RUN.PS1` instead. |
| `FFMPEG.EXE`, `FFPROBE.EXE`, `ARIA2C.EXE`, `yt-dlp.exe`, `deno.exe` | Installer-managed media/download runtime. Presence is not validity; Home and setup probe the executables. |
| `App/` | Payload for the legacy `Bin\__CONTEXTMENU+.exe`, including `YOUTUBE.INI`, FFmpeg, Ghostscript, ExifCleaner and AV1/RTX40 helpers. It is separate from `SAITULS.exe` dependencies. |

## SAITULS GUI

`SAITULS.exe` has six tabs: **Home**, **Explorer menus**, **Accounts**,
**Tools**, **Organizer** and **Settings**. Accounts lists the managed slots
with per-account Auto-connect intent and hosts the global Background
keeper toggle under Advanced. Home reports real Python/media readiness and offers Install /
repair. Explorer menus exposes the 14 registry features, preflights selected
sources, takes `Global\SAITULS_REGISTRY_MUTATION`, and reports the elevated
child's real result. Tools has grouped media, Clipboard+ and other-tool
actions. Settings owns SAITULS autostart, the Taskbar helper lifecycle, open
folder and exit.

Tool launch contracts:

- media actions use `DL_YT.PS1` and `MERGE_AUD.PS1`; output destination is
  chosen before launch and job state/log remains visible;
- Item List / Item Tree use `item_tools.py`; the shell never owns traversal;
- SAIPATCH, Scenarios, Secure Apps, AI Consoles, Shell Doctor, SAISPIN
  Settings and SAIOPT each launch their subsystem entry point and no business
  logic in `SAITULS.cs`;
- Console and scenario launchers are non-elevated; an individual scenario may
  request UAC only when its registry record says `requires_admin`.

## Reliable taskbar edge reveal (`Scripts/taskbar_edge/`)

`TaskbarEdge.exe` is a separate, non-elevated, per-user helper. It samples
`GetCursorPos` and resolves the monitor under the pointer; it never depends on
the foreground window, `WM_MOUSEMOVE` delivery or desktop ownership. Two
consecutive samples in the monitor's bottom edge and the dwell window trigger
one `SetWindowPos(HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
SWP_SHOWWINDOW)` refresh. It does not activate, move, resize or focus anything.
`ABM_GETSTATE` is read at most every two seconds. One instance is enforced by
`Local\SaitulsTaskbarEdge`; `SAITULS.ini` records the user choice and the
per-user Run key is separate.

```
TaskbarEdge.exe --status
TaskbarEdge.exe --self-test
TaskbarEdge.exe --stop
```

## Secure Apps (`Scripts/secure_apps/`)

FIDO2-gated protected applications. Obsidian is the reference profile;
`secure_apps.json` is the fail-closed registry. The broker, GUI and protected
application remain medium integrity. Only the fixed, fingerprinted
`\SAITULS\SecureAppsPrivilegedHelper` scheduled task and its protected runtime
are elevated. The runtime includes a PyInstaller `--onedir` FIDO worker; every
bundle file is hash-bound, and no interpreter or user-owned source path is
placed on the elevated runtime.

`secure_apps.pyw` and `secure_apps_gui.py` are the GUI surface. `sa_cli.py`
exposes headless `selftest`, `status`, `profiles`, `capabilities`, `enroll`,
`import-app`, `open`, `lock`, `mode`, `recover`, `migrate`, `migrate-status`,
`migrate-recover`, `audit` and `privileged-helper` operations. The fixed task
is installed once, then starts silently only after its canonical definition and
runtime fingerprints pass. A tampered task is `PRIVILEGED_TASK_TAMPERED`, not
silently repaired from medium integrity. Migration is a staged, verified
cutover; it reports `MIGRATION_VERIFIED` and `PLAINTEXT_SOURCE_REMAINS` until
the user removes the old copy.

The complete setup, hardware acceptance and recovery contract is in
`Scripts/secure_apps/README.md`.

## AI consoles (`Scripts/consoles/`)

`CONSOLES.ps1` is a data-driven, non-elevated launcher. `consoles.json` uses
schema `saituls.consoles/1`; each profile has a bare command, argument array,
child-only environment map, target mode and title. Enabled profiles in file
order are Claude 1/2, Codex 1/2/3-free, Antigravity, ZCode, Cline and
OpenCode. `CLAUDE_CONFIG_DIR` / `CODEX_HOME` separate account state at launcher
level; this is not a sandbox. `HOME`, `USERPROFILE`, `PATH` and related
environment keys are not overridden by the launcher.

`Installers/INSTALL_CONSOLES_MENU.PS1` creates one HKCU `SaitulsAgents`
cascade for Directory, Directory\Background and Drive, snapshots all three
roots, rolls back on failure, and supports `-VerifyOnly`, `-Uninstall` and
`-RemoveLegacy`. `Add-AgentConsolesMenu.ps1` is the ordinary install wrapper;
`Remove-AgentConsolesMenu.ps1` removes the cascade. The legacy machine-wide
Codex menu is separate.

## SAI Accounts (`Scripts/accounts/`)

Optional control plane for shared agent identities: registry, CLI and broker,
with no dependency of its own on the GUI. A canonical `account_id` is
`<provider>:<locator>:<16 hex>`, and the public `list` projection strips the
Windows SID and exposes only `profile_locator`, `windows_user` and
`context_label`. The GUI surface (SAITULS Accounts tab) and the runtime are
separate. Consumers treat it as federation, never as a prerequisite: control
plane absent → STANDALONE, present → FEDERATED, both together → HYBRID.
Contract: `Scripts/accounts/SAI_ACCOUNTS.md`.

`Scripts/accounts/Install-SaiAccountsKeeper.ps1` registers the one canonical
background keeper task, `SAITULS\SAI Accounts Keeper`: the installed engine
as `sai-accounts autoconnect --budget-ms 90000` at logon and about every two
minutes. The task owns no account state. Switches: `-Enable` (registers and
runs one immediate reconciliation), `-VerifyOnly` (exact-definition check),
`-Disable` (stops the periodic run; running brokers stay online and
per-account intent is untouched), `-Remove`. Registration is transactional
with XML rollback, and an access-denied install fails as categorized
`ELEVATION_REQUIRED` rather than a raw error. Concurrent sweeps serialize on
a named engine mutex — the loser exits `deduplicated` starting nothing — and
every run appends one bounded, secret-free line to `logs/keeper-<scope>.log`
(256 KiB rotation). Only `ENABLED` managed accounts with `auto_connect ==
true` are ever restored. Quota still comes from `snapshot`/`usage` against
broker state, never from a GUI poll. The Accounts Advanced panel holds the
persistent global keeper toggle — distinct from per-account Auto-connect —
with real task-state inspection, last-check reporting and Repair. Tests:
`tests/test_accounts_keeper.ps1`,
`tests/test_managed_console_context.py` (concurrent-sweep checks),
`tests/test_accounts_tab.ps1`, `tests/test_accounts_runtime.ps1`.

## SAIPATCH and migrated tools (`Scripts/saipatch/`)

`patcher.ps1` is generic and patch-agnostic. `opencode-queue-mode` stages an
OpenCode server plugin; it never edits the OpenCode binary. States are
`NOT_INSTALLED`, `AVAILABLE`, `INSTALLED`, `NEEDS_REAPPLY`,
`UNSUPPORTED_VERSION`, `SOURCE_DRIFTED`, `OPEN_CODE_RUNNING`, `BROKEN_PATCH`
and `RESTORABLE`. Apply is plan, backup, stage, staged-hash verify, commit and
post-verify, with rollback on every failure.

`queue.ps1` and `queue_viewer.pyw` expose the read-only Queue Viewer Cockpit;
`queue_manager.pyw` is quarantined behind the explicit `-LegacyCommand` path.
The queue captures complete busy-session submissions, dispatches one item per
authoritative `session.idle`, freezes on retry/error/abort, exposes failed
sends for requeue, and bounds the queue at eight items. Queue state is
in-memory in v1. `clipboard+.pyw` owns clipboard image capture and safe URL
sharing; `_ITEM_LIST.py` and `_ITEM_LIST_RECURSIVE.py` provide the item
listing modes. Full operational contract: `Scripts/saipatch/README.md`.

## Scenarios launcher (`Scripts/scenarios/`)

`SCENARIOS.ps1` is a non-elevated Golden Default WinForms launcher driven by
`scenarios.json` (schema `saituls.scenarios/1`). The registry currently has
24 entries: 17 enabled and 7 disabled with explicit blockers. Script paths
are resolved beneath `Scripts/scenarios`; user input never becomes a command
string. `requires_admin` is per scenario, not launcher-wide. Destructive
scenarios require an explicit confirmation naming action, scope and admin
requirement. Missing dependencies or unverified provenance disable the row
and show the blocker. `MIGRATION.json` keeps dangerous legacy references
outside the runnable set.

## SAISPIN — orphan CPU watchdog

`Scripts/saispin_logic.py` is the pure decision engine. `saispin_watch.ps1`
owns enumeration, sampling, parent identity, durable state, notifications and
the optional kill re-check. `Install-SaispinTask.ps1` registers
`SAITULS\SAISPIN` at logon and every five minutes, hidden, with no parallel
instances; installation remains dry-run until `-AutoKill` is deliberately
armed.

```
powershell -ExecutionPolicy Bypass -File Scripts\saispin_watch.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File Scripts\saispin_watch.ps1 -SelfTest
powershell -ExecutionPolicy Bypass -File Scripts\Install-SaispinTask.ps1 -VerifyOnly
powershell -ExecutionPolicy Bypass -File Scripts\Install-SaispinTask.ps1 -AutoKill
```

Automatic termination requires all of: orphan identity, six consecutive hot
samples over 30 minutes, armed policy, a still-matching process identity and
a non-allowlisted image. Missing evidence resolves to no kill. State/log and
cooldown behavior are in `saispin_settings.ps1` and the watcher.

## Shell Doctor (`Scripts/shell_doctor/`)

`SHELL_DOCTOR.ps1` collects event logs, physical-disk/partition mapping,
Explorer modules, shell-extension registrations and folder/cache state. The
pure ranking engine is `shell_doctor_logic.ps1`. It ranks storage resets and
lost writes, Explorer crashes/hangs, idle/dangling shell extensions,
thumbnail overlap and view-state bloat, then gives concrete fixes.

Flags: `-Days N`, `-Json`, `-NoPause`, and `-ResetViews`. Reset is per-user,
asks for `YES`, exports the Bags registry state first, removes thumbnail/icon
caches, and restarts Explorer. It never writes HKLM, services, power plans or
device settings. Test: `tests/test_shell_doctor.ps1`.

## SAIOPT — background optimizer (`Scripts/saiopt/`)

`SAIOPT.exe` is a tray resident. **Ease** lowers only a qualifying background
process from Normal to BelowNormal while the machine is CPU-busy, then
restores it when the user reaches it, sound starts, activity quiets or the
machine has headroom. It never touches the focused process family, audible
apps, shell/system hosts, service session, or a process already at non-Normal
priority. **Watch** is read-only storage/lost-write/taskbar/Explorer diagnosis.
**Keep awake** performs a periodic uncached read on a disk with the measured
USB idle-reset signature. **Sweep** asks first before ending stale processes;
nothing is armed automatically. SAIOPT never deletes files, trims working sets,
touches services/HKLM, restarts Explorer or elevates.

```
SAIOPT.exe --status
SAIOPT.exe --self-test
SAIOPT.exe --stop
SAIOPT.exe --restore
SAIOPT.exe --shot FILE
```

## `Scripts/_other/`

Standalone WhatsApp maintenance scripts outside the default install set:
`wa-debloat.ps1`, `wa-fix-acl.ps1`, `wa-fix-wns.ps1` and `wa-logon-task.ps1`.
They are reversible and keyed by package family or EXE name, not a build
version. They are not launched by the SAITULS shell.

## Verification entry points

Use repository-owned tests, not invented probes:

- `python tests/test_regs.py`
- `python tests/test_workers.py`
- `python tests/test_del_junk_all_disks.py`
- `python tests/test_destructive_identity.py`
- `python tests/test_saispin.py`
- `python tests/test_scenarios_workers.py`
- `python tests/test_scenarios_launcher.py`
- `python tests/test_consoles_launcher.py`
- `python tests/test_queue_viewer.py`
- `python tests/test_secure_apps.py`
- `python tests/test_secure_apps_acceptance.py`
- `python tests/test_secure_apps_hygiene.py`
- `python tests/test_secure_apps_privtask.py`
- `python tests/test_secure_apps_fido2_contract.py`
- `python tests/test_saituls_tools_ux.py`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_saispin_task.ps1`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_saiopt_logic.ps1`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_saiopt_contract.ps1`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_shell_doctor.ps1`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_taskbar_edge_logic.ps1`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_taskbar_edge_contract.ps1`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_import_safe.ps1`
- `python tests/test_sai_accounts.py`
- `python tests/test_managed_console_context.py`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_accounts_keeper.ps1`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_accounts_tab.ps1`
- `powershell -NoProfile -ExecutionPolicy Bypass -File tests/test_accounts_runtime.ps1`
