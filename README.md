# SAITULS

![Windows](https://img.shields.io/badge/Windows_10/11-x64-00ADEF?style=flat&logo=windows&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-blue?style=flat)
**v0.2.0**
![CI](https://img.shields.io/github/actions/workflow/status/vacterro/saituls/test.yml?style=flat&label=tests)
![C#](https://img.shields.io/badge/C%23-9B4993?style=flat&logo=csharp&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white)

**SAITULS** adds utility commands to Windows File Explorer and bundles file, media and Codex tools in one window. The installer fetches missing Python, FFmpeg, yt-dlp, aria2 and Deno automatically.

Version 0.2.0 is a **prerelease**. Secure Apps requires its per-machine hardware
acceptance before protecting real data. SAIPATCH supports only the exact OpenCode
builds listed in its manifest; a newer installed CLI is not automatically supported.

Download `SAITULS-Windows-0.2.0.zip` from the release for the complete application,
or combine the source ZIP and the matching payload ZIP. When upgrading Secure Apps,
preserve your configured `storage.mount_path` in `Scripts/secure_apps/secure_apps.json`;
the new default is `%USERPROFILE%\Documents\SAITULS Vaults\Obsidian`.

---

## ✨ Features

| | Category | What |
|---|---|---|
| 🖱️ | **14 Explorer commands** | Copy path, delete duplicates/empty/junk/same-name, YouTube download, FFmpeg conversion, MKV repair, project scaffolding, pack, PowerShell admin, take ownership, toggle hidden files |
| 🐌 | **SAISPIN** | Orphan CPU watchdog — finds processes that lost their parent and kept burning a core; alerts by default, kills only when orphanhood *and* sustained spin are both proven |
| 📏 | **Taskbar edge reveal** | With auto-hide on, pushing the pointer into a monitor's bottom edge reveals *that* monitor's taskbar — even under a maximized or always-on-top window. Tiny non-elevated helper, 0 % idle CPU, never steals focus |
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

Requires **Windows 10/11 x64** and internet on first install. Python, FFmpeg, yt-dlp, aria2 and Deno are installed automatically.

Normal launch after install — `SAITULS.exe` or `SAITULS_LAUNCHER.cmd`. The program runs **without elevation**; UAC only appears where Windows genuinely requires it (e.g. menu installation).

---

## Main window

| Tab | Purpose |
|---|---|
| **Home** | Shows what is missing and offers one-click install/repair |
| **Explorer menus** | Enables or removes selected Explorer commands — whole-row click targets |
| **Tools** | Launches utilities from one place, asks for target first |
| **Settings** | Opens program folder, full exit, optional silent tray autostart, and the **Taskbar** section for the reliable bottom-edge reveal |

The close button hides the window to the tray rather than terminating the background controller. Use **Settings → Exit** or the tray menu **Exit** for a full shutdown.

### Taskbar edge reveal

**Settings → Taskbar** turns on `Reliable taskbar edge reveal`. It is off until you enable it once — upgrading SAITULS never switches it on. With taskbar auto-hide enabled, pushing the pointer against the bottom edge of any monitor reveals that monitor's taskbar, including when a maximized or always-on-top window owns the edge pixels.

The work is done by `Scripts\taskbar_edge\TaskbarEdge.exe`, a separate non-elevated per-user background helper with no window of its own. Its lifetime is independent of SAITULS: hiding to the tray or exiting SAITULS leaves it running, and the same Settings switch is the explicit stop. `Start with SAITULS / Windows` adds the per-user Run entry. Measured 20/20 reveals per monitor with 0 % idle CPU; Windows keeps owning the hide animation, and the foreground application never loses focus. Details and advanced settings: [`Scripts/taskbar_edge/README.md`](Scripts/taskbar_edge/README.md).

Keyboard: `Ctrl+1`…`Ctrl+4` switches tabs, `Tab`/`Shift+Tab` moves focus, `Enter`/`Space` activates, `Esc` hides the window.

---

## 14 Explorer commands

| Command | Action |
|---|---|
| Copy file path | Copies the full path of the selected file as text |
| Delete duplicates | Finds identical files by size and SHA-256, lists, keeps first copy, deletes rest after confirmation |
| Delete empty folders | Recursively removes empty directories after confirmation, logs to `Scripts\_.txt` |
| Delete junk | Drive roots automatically use conservative `SAFE_DISK`; selected folders retain aggressive project cleanup. Shows progress, reclaimable bytes and a saved manifest before permanent deletion |
| Delete junk on all disks | One `SAFE_DISK` streaming pass over every local fixed disk (network, removable, optical and RAM drives are skipped). Explicit start confirmation, per-candidate durable journal, fail-stop on integrity loss |
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
- **Scenarios** — opens the dedicated scenario launcher (below)
- **Secure Apps** — opens the FIDO2-gated protected-application launcher (below)
- **AI Consoles** — opens a non-elevated console for Claude 1, Claude 2 or Antigravity (below)
- **Shell Doctor** — read-only diagnosis of Explorer / Start lag: USB storage resets, shell crashes, idle shell extensions, thumbnail-handler overlap, folder-view bloat

If a component is missing, SAITULS does not launch a broken command; it sends the user to **Home → Install / repair**.

---

## Secure Apps

`Scripts\secure_apps\` is a FIDO2-gated launcher for applications whose data
should not be readable when the application is closed. Obsidian is the
reference profile; the architecture is profile-driven, so further protected
applications are added through `secure_apps.json`, not new code. The window
follows the same Golden Default contract as the rest of SAITULS.

- **Two clocks, deliberately separate.** In the default policy the
  *authentication session* survives six idle hours, but the *vault is
  detached the moment the protected application exits*. Reopening inside the
  session remounts without another key touch; the data was not readable in
  between. Aggressive mode invalidates the session the instant the
  application exits. "Idle" means six hours without **protected** activity —
  a protected launch, the protected application in the foreground, or a
  Secure Apps action — not six hours without mouse movement elsewhere.
- **Key hierarchy.** FIDO2 `hmac-secret` → HKDF-SHA256 with profile context →
  AES-256-GCM unwrap of a random BitLocker volume secret, with the wrapped
  key bound to profile id, container id, credential id and schema version.
  Only ciphertext and public metadata reach disk. An authenticator without
  the CTAP2 `hmac-secret` extension is **refused**, never downgraded to a
  presence check. A second security key can be enrolled without re-encrypting
  the vault, and the BitLocker recovery material is shown exactly once and
  stored nowhere by SAITULS.
- **Possession of the key is not enough.** The reference profile sets
  `authentication.user_verification: required` (the default), so enrollment
  and every unlock need FIDO2 PIN or built-in verification as well as a
  touch, and an assertion without the UV bit is refused. The PIN is never
  stored, logged, or passed through a command line or environment variable.
- **The transport is reported, not assumed.** `WINDOWS_WEBAUTHN` (preferred:
  the broker is deliberately non-elevated) or `DIRECT_CTAP`, or a plain
  `UNAVAILABLE` that says which precondition failed. Written against
  python-fido2 2.x, with the supported range pinned and checked at load.
  Run `python Scripts\secure_apps\sa_cli.py capabilities obsidian` to see
  what a host can really do -- notably, Windows 10 22H2 ships a
  `webauthn.dll` too old to carry an hmac-secret salt.
- **First run does not need the vault path.** Enrollment builds the
  encrypted container at its own staging mount and leaves it locked and
  detached, so it runs while the plaintext vault is still in place; the real
  mount path is claimed only at the verified cutover, and every mount target
  is proven empty before anything is created.
- **Storage.** A BitLocker XTS-AES-256 NTFS volume inside a data VHDX,
  mounted onto a directory mount point so the vault keeps its historical
  path. The unlock secret never touches a command line, an environment
  variable, a temp file or a log: the non-elevated broker passes it to a
  short-lived elevated helper over a named pipe.
- **One consent dialog, at setup, and none afterwards.** Attaching a VHDX
  needs administrator rights; opening Obsidian must not have them. The
  elevation is approved once, for one **fixed** Scheduled Task action
  (`\SAITULS\SecureAppsPrivilegedHelper`), whose canonical definition is
  fingerprinted and re-checked before every silent start — a task whose
  action, principal, run level or arguments moved is refused as
  `PRIVILEGED_TASK_TAMPERED`, not repaired. After that, unlock, lock, mount
  and relock raise **no UAC prompt at all**, while global UAC stays on and
  untouched for every other program. Starting the helper is not the same as
  trusting it: the broker proves from the kernel that the process answering
  the pipe is high integrity, running the pinned image, as this account, in
  this session. The helper exits on its own after five idle minutes and the
  next unlock silently starts it again. You still touch the security key —
  FIDO2 user verification is not Windows elevation.
- **Relock is a sequence, not a flag.** Graceful close → bounded wait →
  optional terminate → **confirmed process-tree exit** → flush → unmount →
  zeroize. Storage is never detached while a process still holds a handle.
  Workstation lock, logoff, suspend, shutdown and Lock Now all route through
  it, under per-profile policy.
- **A crashed broker is not a locked vault.** Reconciliation surfaces
  `RECOVERY_REQUIRED` and refuses new launches until a safe relock completes.
- **Migration never deletes your only copy.** The plaintext vault is copied,
  verified (tree, byte sizes, representative hashes, a real application open,
  a lock/unlock round trip and a re-verify) and only then cut over by
  *renaming the original aside*. The result is reported as
  `MIGRATION_VERIFIED` **and** `PLAINTEXT_SOURCE_REMAINS` until you remove
  the old copy yourself.

Full contract, protocols and first-run steps: `Scripts\secure_apps\README.md`.

---

## AI consoles

`Scripts\consoles\` opens interactive agent CLIs in a chosen project folder:
`Claude 1` and `Claude 2` (same `claude` command, different child
`CLAUDE_CONFIG_DIR`) and `Antigravity CLI` (`agy`). The registry stores a
bare command name and an argument list, so nothing in it can become a shell
command.

These consoles are **not elevated**, and that is deliberate: the
Explorer-menu OpenCode/Cline launcher self-elevates to maximum privilege and
this subsystem does not inherit it. The command is resolved on PATH before
launching, the title is project-first, Ctrl+C and Ctrl+V keep working, and a
nonzero exit keeps the window open. Claude account isolation is at launcher
level — upstream Claude Code may still read user-level instructions from
outside `CLAUDE_CONFIG_DIR`.

Details: `Scripts\consoles\README.md`.

---

## Scenarios launcher

The migrated utility collection lives in `Scripts\scenarios\` and is reached
through the single **Scenarios** button in the Tools tab, which opens
`Scripts\scenarios\SCENARIOS.ps1` — a compact data-driven WinForms window
(Golden Default, ~520×430) with a category filter, instant search, safety
badges (SAFE / MODIFIES FILES / ADMIN / DESTRUCTIVE / LONG RUNNING) and
Run / Open folder / Close actions.

- **Data-driven registry.** `Scripts\scenarios\scenarios.json` defines every
  scenario (id, label, category, script, interpreter, badges, target mode,
  dependencies). Script paths resolve only beneath `Scripts\scenarios` — path
  escapes are refused.
- **Destructive confirmation.** Every scenario marked destructive shows an
  explicit confirmation naming the action, admin requirement and scope before
  anything runs. No auto-confirm.
- **Admin is per-action.** The launcher itself never runs elevated; only a
  scenario marked `requires_admin` requests UAC when run.
- **Dependency gates.** A scenario whose bundled or external dependencies are
  missing (or whose provenance is unverified) is listed as disabled with the
  exact blocker instead of pretending to work.
- **Legacy safety.** Genuinely dangerous legacy scripts (vendor cleanup,
  Office scrubbers, defrag, session nuking) are classified
  `LEGACY_REFERENCE_ONLY` in `Scripts\scenarios\MIGRATION.json` and are not
  exposed as runnable. The original external collection is untouched
  reference material; no migrated tool depends on it.

---

## DEL JUNK ALL DISKS — unattended destructive behavior

`DEL JUNK.PYW --all-disks` cleans every local fixed disk with the conservative
`SAFE_DISK` policy only. The normal run is **streaming and unattended**, with
these guarantees:

- **Explicit start confirmation.** Before anything destructive the worker
  tests the journal destination, runs a runtime safety gate on throwaway
  fixtures, inspects the previous run's journal, and shows one confirmation
  dialog. Nothing is deleted before the user explicitly starts. Cancel means
  zero deletions, zero journal INTENT records.
- **Runtime safety gate.** `all_disks_destructive_ready()` proves ten named
  clauses in-process (SAFE_DISK policy integrity, protected subtrees, identity
  replacement for files and directory trees, real junction boundaries,
  overlapping-scope locks, journal durability, INTENT-before-delete ordering,
  outcome-failure fail-stop, cancellation truth). A blocked gate means zero
  deletion, no override, exit code 3.
- **Durable journal.** Every candidate gets a durable `INTENT` record
  (flushed and fsynced) before deletion, and a truthful terminal outcome
  (`DELETED` / `SKIPPED_CHANGED` / `RISKY_EXCLUDED` / `REFUSED` / `FAILED`) after.
  Run-level events `RUN_START` / `DRIVE_START` / `DRIVE_END` / `RUN_END` frame
  the run; a compact `stream-*.summary.json` lands next to the journal.
- **Fail-stop.** Journal loss, an unexplained destructive I/O failure, or an
  internal invariant violation stops the run before the next candidate and
  before any later drive. The journal is the only truth: a crash after INTENT
  but before the outcome is reported as a dangling intent
  (`UNKNOWN_AFTER_INTENT`), never as a deletion.
- **Cancellation is not rollback.** Closing the progress window or pressing
  Cancel stops before the next candidate; already-deleted data stays deleted.
  A partial destructive run exits with code 2, never 0.
- **`--preview` is zero-deletion.** It scans, saves manifests and reports
  candidates and risky exclusions; it never deletes and never writes INTENT.
- **Journal location:** `%LOCALAPPDATA%\SAITULS\DEL_JUNK` (plus `manifests\`
  and `%TEMP%\SAITULS\DEL_JUNK\locks`). These are never candidates for
  deletion by the worker itself.

Exit codes for `--all-disks`: `0` completed normally or nothing to do; `1`
user cancelled before mutation; `2` runtime/fatal error or partial destructive
run; `3` the safety gate blocked the run.

---

## SAISPIN — orphan CPU watchdog

Some processes lose the thing that was supposed to shut them down — the editor
that spawned them, the shell that was waiting on them — and then keep burning a
CPU core forever. They do not crash, nothing reports them, and the only symptom
is a fan that never stops. SAISPIN looks for exactly that, and nothing else.

```powershell
powershell -ExecutionPolicy Bypass -File Scripts\saispin_watch.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File Scripts\Install-SaispinTask.ps1
```

The first line runs one sweep and terminates nothing. The second registers the
scheduled task `SAITULS\SAISPIN` to sweep at logon and every five minutes,
hidden — still in dry-run, so installing the watchdog cannot by itself end a
process. Add `-AutoKill` to either command to arm it; `-Remove` unregisters the
task, `-VerifyOnly` reports what is registered without touching it.

What it takes to be terminated automatically, all of it at once:

- **orphan** — the parent is gone. A live process holding the parent's PID is
  checked against its own start time first: a "parent" younger than its child
  just inherited the number and does not count
- **sustained spin** — six *consecutive* hot samples (>80% of one core) spanning
  at least 30 minutes. One cold sample restarts the count from zero
- **auto-kill armed** — off by default
- **identity still matching** — re-read immediately before the kill, so a
  recycled PID cannot be mistaken for the process that was judged
- **not allowlisted** — kernel PIDs, the Windows service processes and the root
  process of a guarded image (VS Code) are never terminated automatically

Orphanhood alone is not enough, and neither is a pegged core: an orphaned
`pytest` sitting at 0% is left alone, and a compiler pegging a core with a live
parent is reported but never touched. Everything the watchdog notices goes to
`saispin.log` with the identity, the numbers and the verdict; alerts also appear
as a tray balloon. Because the sweep repeats every five minutes and a runaway can
spin for hours, a balloon for the same process is shown once and then not again
for an hour — unless the verdict itself changes, which always breaks through. When
several processes cross the line in one sweep they share a single balloon that
names the count instead of stacking one balloon per process. The
log still records every sweep, so nothing is hidden by the quiet. Missing or
unreadable information always resolves toward not killing, and a state file that
had to be discarded downgrades every kill to an alert.

The decision logic lives in `Scripts\saispin_logic.py` with no Windows API in
it, so the rules can be checked directly:

```cmd
python tests\test_saispin.py
powershell -ExecutionPolicy Bypass -File Scripts\saispin_watch.ps1 -SelfTest
```

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

This removes Explorer entries but does not delete your files or the SAITULS folder. Before relocating, close SAITULS, move the entire folder, then re-run `INSTALL.cmd` to update registry paths.

The installer records which language generation it applied in `Installers\.installed_lang`, so `-Uninstall` without `-Lang` removes the generation that is actually installed, and switching language (`-Lang et`) removes the previous one before applying the new one instead of leaving both menu trees behind.

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

Source file — `SAITULS.cs`; a ready-to-run EXE sits next to it so a regular user does not need a compiler. Repository checks:

```cmd
python tests\test_regs.py
python tests\test_workers.py
python tests\test_saispin.py
powershell -ExecutionPolicy Bypass -File tests\test_locale_generation.ps1
powershell -ExecutionPolicy Bypass -File tests\test_codex_menu_preflight.ps1
powershell -ExecutionPolicy Bypass -File tests\test_shell_menu.ps1
powershell -ExecutionPolicy Bypass -File tests\test_import_safe.ps1
powershell -ExecutionPolicy Bypass -File tests\test_self_elevation_result.ps1
```

`test_workers.py` covers the destructive Explorer commands. Both of its cases are
real: deleting a duplicate that stopped being one during the confirmation dialog,
and an unreadable directory aborting a delete-empty run after it had already
removed something. Neither leaves anything in the Recycle Bin, so they are tested
by mutating the filesystem from inside the confirmation hook.

`test_locale_generation.ps1` runs the real `INSTALL_ALL.PS1` against a throwaway
tree with `reg.exe` replaced by a recorder, so it can prove which language
generation is imported and in what order without touching your registry. It also
runs `i18n\tools\gen_locale_reg.py` against a throwaway project tree of its own,
because that generator publishes what the installer then imports: `--out PATH`
must land in `PATH`, an unknown flag must exit nonzero, and a missing label or
bundle key must exit nonzero while leaving the previous generation byte-identical.
`test_codex_menu_preflight.ps1` does the same for the Codex cascade installer
against a throwaway `HKCU` key: a refused install must leave the existing menu
untouched. `test_shell_menu.ps1` builds
a verb from the shipped `.REG` command line and invokes it through the real shell,
proving a menu entry both starts and receives the correct target — including a
drive root. `test_import_safe.ps1` imports through `IMPORT_SAFE.PS1` and reads the
value back, because `reg.exe` exits 0 even when it skipped a line.
`test_self_elevation_result.ps1` runs the two self-elevating scripts with
`Start-Process` stubbed, because a script that relaunches itself as administrator
can only report the launch — it must wait for that elevated child and exit with
its code, so a cancelled UAC prompt is never mistaken for a finished install.

Build on Windows with the built-in .NET Framework 4.x compiler:

```cmd
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe -nologo -target:winexe -out:SAITULS.exe -optimize+ -r:System.dll -r:System.Drawing.dll -r:System.Windows.Forms.dll SAITULS.cs
```

The `i18n\strings` folder holds 33 string bundles. The ready-made registry locale is currently included for Estonian; others are generated on demand via `i18n\tools\gen_locale_reg.py <locale> [--out DIR]`. The generator is fail-closed: a mapped label that is missing from its `.reg`, or a bundle key that no longer exists, exits nonzero and publishes nothing, and a successful run replaces the destination directory atomically rather than writing into it file by file.

**License** — MIT, see `LICENSE`.

---

## Repository structure

```
SAITULS/
├── SAITULS.cs / SAITULS.exe     # Main control-panel (WinForms)
├── Registry/                     # 14 context-menu .reg files + REM counterparts
├── Scripts/                      # Batch, Python, PowerShell workers
│   ├── saispin_logic.py          # Orphan-spin decision engine (pure, no Windows API)
│   ├── saispin_watch.ps1         # SAISPIN sweep: sample, decide, alert, optional kill
│   └── Install-SaispinTask.ps1   # Registers SAITULS\SAISPIN (dry-run by default)
├── Installers/                   # INSTALL_ALL.PS1, agent-menu installers
├── i18n/                         # 33-locale strings, docs, registry locale
│   ├── strings/                  # 33 × 69-key translation bundles
│   ├── docs/                     # 33-locale documentation
│   └── reg/et/                   # Estonian registry locale
├── Wiki/                         # 4-page project wiki
├── tests/test_regs.py            # Integrity suite
├── tests/test_workers.py         # Destructive-worker regressions (DEL_DUP, DEL_EMPTY)
├── tests/test_saispin.py         # SAISPIN decision-engine self-check
├── tests/test_locale_generation.ps1  # Installer locale-generation behaviour
├── tests/test_codex_menu_preflight.ps1  # Codex cascade install preflight
├── tests/test_shell_menu.ps1     # Explorer menu launch + argument contract
├── tests/test_import_safe.ps1    # %%ROOT%% reg import actually writes
├── tests/test_self_elevation_result.ps1  # Elevated child result reaches the caller
├── Scripts/saipatch/               # SAIPATCH: OpenCode patch manager (Tools tab)
├── Scripts/secure_apps/            # Secure Apps: FIDO2-gated protected applications (Tools tab)
├── Scripts/consoles/               # AI consoles: non-elevated Claude 1/2 + Antigravity launcher
├── Scripts/shell_doctor/           # Shell Doctor: read-only Explorer/Start lag diagnosis (Tools tab)
├── Scripts/taskbar_edge/           # TaskbarEdge: reliable bottom-edge taskbar reveal helper
├── INSTALL.cmd / setup.ps1      # One-click installer
├── CHANGELOG.md                  # Release history
└── LICENSE                       # MIT
```

Heavy binaries (ffmpeg ~153 MB ×2, ffprobe, yt-dlp, AV1/RTX40 builds, Ghostscript, ExifCleaner) are not committed. Download the payload zip from the latest release and extract into `Bin\` / `Bin\App\` — or run `BUILD_PAYLOAD.cmd` against a full local tree. The builder takes its release version from `VERSION`, packages exactly `PAYLOAD_MANIFEST.txt`, validates the staged ZIP, and only then replaces the matching artifact under `dist\`.
