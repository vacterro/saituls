## 0.2.0 — 2026-09-23 (prerelease)

### Release preparation

- Portable defaults for Clipboard+, Obsidian vaults and legacy YouTube downloads.
  Existing installations should retain their configured vault mount path when upgrading.
- Secure Apps imports portable Obsidian from a selected folder; cancellation does nothing.
- Console diagnostics continue when an optional CLI script is missing, including an unset
  `ZCODE_CLI`; OpenCode diagnostics report the port actually passed to the process.
- Batch image tools require an explicit target; personal library paths are excluded.
- Console tests supply disposable CLI fixtures, Ctrl+V probes use the system temporary
  directory, and clean-package verification accepts explicit source and payload archives.
- Hardware Secure Apps acceptance and live supported-build OpenCode acceptance remain
  required before declaring those integrations validated on a particular machine.

## Added

- **One Explorer cascade for every agent console (`Installers/INSTALL_CONSOLES_MENU.PS1`).**
  Right-click a folder, its background or a drive -> "Открыть в" -> Claude 1,
  Claude 2, Codex 1, Codex 2, Codex 3 free, Antigravity, ZCode, Cline, OpenCode.
  Every item opens a plain, non-elevated PowerShell console running
  `Scripts/consoles/CONSOLES.ps1` for that profile in the clicked folder, and
  every profile starts in its CLI's permission-skipping ("YOLO") mode. The
  cascade is generated from `consoles.json` (file order, `menu_label`), lives
  in HKCU only, rolls back on failure and supports `-VerifyOnly`/`-Uninstall`.
  `Add-AgentConsolesMenu.ps1` installs it and drops the older flat
  OpenCode/Cline entries it replaces; `Remove-AgentConsolesMenu.ps1` removes it.
- **`consoles.json` profiles:** Codex 1/2/3-free (isolated `CODEX_HOME`,
  inherited API keys removed for the secondary accounts), ZCode, Cline and
  OpenCode join Claude 1/2 and Antigravity. New per-profile fields:
  `{workdir}` and `%VAR%` inside arguments, an empty environment value that
  removes the variable from the child, `requires_path`, `ensure_directories`,
  `icon` and `script_candidates` (first existing path becomes `{script}`; ZCode
  uses it to find a full ZCode CLI build).

- **Shell Doctor (`Scripts/shell_doctor/`, Tools tab).** Read-only diagnosis
  of Explorer / Start lag, built from a real incident: a USB NVMe enclosure
  (JMicron bridge, `UASPStor`) logging `Event 129` resets every ~285 s froze
  the shell for seconds at a time. The tool maps storage resets to the
  physical disk and flags regular cadence, counts `explorer.exe` crashes and
  hangs, lists shell extensions whose owning app is not running, dangling
  handler registrations, overlapping thumbnail providers and folder-view
  bloat, ranked by severity with a concrete fix each. The one mutation,
  `-ResetViews`, is per-user (Bags/BagMRU + thumbnail/icon caches), asks
  first, backs up first; HKLM, services, power and devices are never written.
  `tests/test_shell_doctor.ps1` (fixtures + read-only collector smoke) runs
  in CI on Windows PowerShell 5.1 and PowerShell 7.

- **Secure Apps: silent privileged operations, without disabling UAC
  (`Scripts/secure_apps/sa_privtask.py`).** The elevated storage/FIDO helper is
  no longer started through a consent dialog at runtime. It is started by a
  Scheduled Task, `\SAITULS\SecureAppsPrivilegedHelper`, registered once
  during setup while the user explicitly approves one elevation, with a
  **fixed** action (`powershell.exe … -File <pinned>\sa_storage_helper.ps1
  -ScheduledHelper`), `RunLevel HighestAvailable`, `LogonType InteractiveToken`,
  no triggers and no caller-supplied arguments. Afterwards a medium-integrity
  broker may start that task — and only that task — with no prompt: open,
  close, lock, relock, workstation lock, suspend and shutdown are all silent.
  Global UAC is untouched (`EnableLUA` stays 1; `ConsentPromptBehaviorAdmin`
  and `PromptOnSecureDesktop` are read for diagnostics and never written), and
  nothing in the stack is elevated except the helper and its CTAP worker.
  The task's canonical definition (action image, argument tokens, working
  directory, principal SID, run level, logon type, triggers, action count, and
  the settings that could starve or kill the helper) is fingerprinted, pinned
  at install time and re-checked before every silent start; a definition that
  moved is `PRIVILEGED_TASK_TAMPERED` and fails closed rather than being
  repaired from a medium-integrity process. What a fixed action cannot carry —
  the protected runtime root, the installed helper, the frozen FIDO worker
  bundle, the pipe name — moves into an installer-written pin in
  `%ProgramData%\SAITULS\secure-apps`, ACL'd and **owned** by
  Administrators/SYSTEM and carrying a SHA-256 per file, which is strictly
  tighter than the old `-PythonExe` command line a medium-integrity process
  controlled on every spawn. (The pin records no interpreter and no worker
  source path: see the protected-runtime entry below for the contract that
  actually shipped.) The pipe direction flipped with it: the elevated helper owns the pipe
  (a per-user name derived from the account SID) and the broker connects, so
  the broker now verifies the *server* from the kernel
  (`GetNamedPipeServerProcessId`, pinned image, `TokenElevation` + integrity
  level, token user, session, greeting cross-check) while the helper verifies
  the client (same account, **medium** integrity — an elevated broker is
  refused). Helper lifetime is a lease: held while a volume is attached or a
  privileged transaction is in flight, then dropped, and the helper exits by
  itself after `privileged_helper_idle_timeout` (default 300 s) with nobody
  connected; the next unlock starts it again silently. Locking never depends on
  consent — when the helper genuinely cannot be started the broker surfaces
  `LOCK_PRIVILEGED_HELPER_UNAVAILABLE` and an ERROR state instead of reporting
  LOCKED over a volume that may still be mounted.
- **Secure Apps setup operations: Install / Repair / Remove / Check privileged
  helper**, in the Secure Apps window and as
  `sa_cli.py privileged-helper <action>`, plus a UAC preflight report
  (`EnableLUA`, broker integrity, admin membership, task installed / valid /
  runnable, definition fingerprints, pin writability, helper integrity, pipe
  authentication) that requires no particular UAC slider position. Install is
  two-sided on purpose: the elevated child registers the task, and the
  medium-integrity process then proves the result is really a silent boundary
  (definition valid, helper started by Task Scheduler, helper high integrity,
  broker still medium) and stops the helper again if it is not. Removing the
  helper removes only the elevation mechanism — never the vault, the FIDO2
  credential, Obsidian data, the acceptance record or recovery material. For a
  host that has `EnableLUA=0`, a separate explicit
  `privileged-helper prepare-uac --confirm` sets `EnableLUA=1`, warns that
  Windows must be restarted, and changes no other UAC setting.
- **Secure Apps: the privileged runtime is protected, fingerprinted whole, and
  installed transactionally.** The shipped contract for the elevated side is
  now: a protected runtime root under
  `%ProgramData%\SAITULS\secure-apps\privileged` (SYSTEM and Administrators
  full control, the intended user read+execute, inheritance off, owner
  Administrators) holding `sa_storage_helper.ps1`, `privileged-runtime.json`
  and a `fido-worker\` bundle; a pin owned by Administrators; a protected
  scratch root `privileged-tmp\`; and a fixed pipe identity derived from the
  account SID.
  - **The FIDO worker is a PyInstaller `--onedir` bundle, never `--onefile`.**
    A onefile executable unpacks its whole executable dependency closure into
    an ordinary `%TEMP%\_MEIxxxxxx` directory and loads it from there; running
    that elevated would mean an elevated process loading its code out of a
    directory the medium-integrity user owns. The complete `fido-worker\` tree
    is installed under the protected ACL and nothing is extracted at runtime.
  - **`fido_worker_bundle_fingerprint`**: a recursive, deterministic manifest
    binding every file in the bundle by relative path, size and SHA-256
    (paths relative to the bundle root, `/` separators, lowercased, printable
    ASCII, ordinal sort, canonical JSON, no timestamps and no build-machine
    paths). Any modification, addition or deletion inside the bundle — a
    swapped DLL as much as a swapped `.exe` — invalidates installation and
    acceptance. Reparse points anywhere inside the bundle are refused. The
    same value is computed identically by `sa_privtask.bundle_fingerprint`,
    by the elevated helper's `Get-BundleFingerprint` and by
    `build_frozen_worker.ps1`.
  - **No user-controlled PowerShell module discovery in the elevated helper.**
    Before any Storage or BitLocker command can auto-load, the helper rebuilds
    `$env:PSModulePath` from machine locations only (resolved through the
    known-folder API, never the environment block), imports `Storage` and
    `BitLocker` from absolute system paths, confirms each module really came
    from `%SystemRoot%\System32\WindowsPowerShell\v1.0\Modules\<name>`, and
    fails closed otherwise. Storage and BitLocker commands are written
    module-qualified. `-SelfTest` reports `trusted_module_path`,
    `storage_module_trusted` and `bitlocker_module_trusted`.
  - **External executables are pinned.** `diskpart.exe` (and every other
    external image the high-integrity helper launches) is resolved to
    `%SystemRoot%\System32\…` and verified to exist; nothing is selected
    through `PATH`.
  - **Privileged command material never sits in user TEMP.** The DiskPart
    script an elevated DiskPart executes is written to a per-transaction
    directory under `%ProgramData%\SAITULS\secure-apps\privileged-tmp`
    (SYSTEM and Administrators only, owner Administrators), and its
    containment, reparse-point freedom, DACL and owner are re-checked in the
    instant before DiskPart opens it. The task XML `schtasks /Create` consumes
    moved there too.
  - **Install and Repair are real transactions.** The previous runtime, pin,
    task XML, task security descriptor and task state are snapshotted before
    the first mutation; a failure at any of the ten stages (stage copy,
    runtime ACL application, runtime ACL measurement, runtime swap, task
    registration, task SD application, pin save, pin hardening, final
    verification, commissioning) rolls back to that snapshot and re-verifies
    the result — a first install to no task, no pin and no protected runtime,
    a repair to the exact previous installation. A final verification that
    does not pass is a failed transaction rather than a `REPAIR_REQUIRED`
    warning over a live task. `harden_runtime_dir()` returning True and
    `runtime_acl_report()["runtime_acl_valid"] is True` are required before
    the task is registered, and re-proven after the canonical move; an
    unmeasurable ACL counts as failure, never as success.
  - **Pin ownership, not only its DACL.** An object's owner holds `WRITE_DAC`
    implicitly, so `%ProgramData%\SAITULS\secure-apps` and
    `privileged-helper.json` are re-owned to Administrators and the intended
    user is granted read through its own SID rather than through
    `Authenticated Users`. Verification reports `pin_owner_valid`,
    `pin_medium_writable` and `pin_medium_can_change_dacl`, and an unreadable
    owner fails closed.
  - **The task FOLDER is part of the boundary.** Deleting a registered task
    needs write access to the folder that contains it, not to the task object:
    a real medium-integrity `schtasks /Delete` succeeded against a task whose
    own descriptor denied DELETE, because `\SAITULS` had inherited the Task
    Scheduler root's permissive ACL. The folder now carries the same explicit
    descriptor as the task, and both are written with
    `TASK_DONT_ADD_PRINCIPAL_ACE` — without that flag Task Scheduler appends
    its own ACE for the run-as account and silently drops the supplied
    `SE_DACL_PROTECTED` flag.
  - **Fixed: every correctly hardened runtime measured as writable.**
    `path_is_writable_by_user` tested its mask with a bitwise AND while the
    mask included the composite rights `FILE_GENERIC_WRITE` and
    `FILE_ALL_ACCESS`, both of which carry `READ_CONTROL` and `SYNCHRONIZE` —
    so an ordinary read+execute ACE matched and `runtime_acl_valid` could
    never be true. The mask now names only rights that actually mean "can
    change it".
  - **Fixed: the task security descriptor is no longer applied through a
    single optional dependency.** The Task Scheduler SD is reachable only
    through the Task Scheduler COM API; when pywin32's COM layer is missing or
    damaged, the same API is now driven through Windows PowerShell at its
    pinned System32 path, so a privileged installer does not fail closed on a
    broken optional package.
- **Secure Apps acceptance: gates with no evidence can no longer pass.**
  `AcceptanceRun.gates()` aggregated each gate with `all(...)` over the checks
  mapped to it — and `all([])` is True, so `privileged_task_security_accepted`
  and `privileged_runtime_acl_accepted`, which had no checks mapped to them at
  all, were recorded as accepted on every run without a single check ever
  having been run. A gate with zero mapped checks is now never accepted, a
  structural test fails the moment a future gate is added without evidence,
  and three real checks were added:
  - `P05` (→ `privileged_runtime_acl_accepted`) attempts, from the real
    medium-integrity broker, to overwrite / delete / rename the protected
    helper, write and delete the FIDO worker, add files to the worker bundle
    and to `_internal`, write the runtime manifest, write the pin and rewrite
    the pin's DACL — every one of which must come back denied by Windows;
  - `P06` (→ `privileged_task_security_accepted`) attempts, against the real
    registered task, query and run (which must be allowed) and change,
    disable, delete and re-secure it, plus creating a task in the privileged
    folder (which must all be denied);
  - `P07` (→ `privileged_runtime_acl_accepted`) proves the pin's owner is
    Administrators/SYSTEM, the protected scratch root is not medium-writable,
    and the installed worker bundle still hashes to the recursive fingerprint
    pinned at installation.
  A mutation that unexpectedly succeeds is a failure, is restored immediately,
  and is reported as unrestored when it could not be.
- **`tests/sa_privboundary_probe.py`**: real Windows access probes run through
  a genuine medium-integrity token — the interactive shell's own filtered
  token where that is medium, otherwise a filtered token built the way UAC
  builds one (Administrators deny-only, removable privileges dropped,
  integrity lowered to Medium) — so an elevated test session can still ask
  Windows the question a medium process would. Outcomes are `DENIED`,
  `ALLOWED` or `ERROR`; an `ERROR` is never evidence of a boundary.
- **`tests/test_secure_apps_privtask.py`**: ten-stage fault injection over the
  install transaction, asserting for every stage that a first install leaves
  no runnable task, no pin and no protected runtime, and that a repair leaves
  the previous installation byte-for-byte back and verifying; bundle tamper
  cases (changed `.exe`, changed DLL inside `_internal`, added file, deleted
  file); refusals for an unprovable runtime ACL, an unprovable pin owner, an
  unprotected scratch root and a failed final verification; and, on a host
  with the runtime really installed, the DESCRIPTOR checks alongside the
  WINDOWS-ACTUALLY-DENIED attempts, including a negative-authorization test
  proving a medium process cannot modify a prepared DiskPart script before
  privileged consumption.
- **Hardware acceptance now binds the elevation mechanism** (record schema 3):
  `privileged_launch_mode`, `privileged_task_name`,
  `privileged_task_definition_fingerprint` and a new gate flag
  `silent_privileged_start_accepted` (checks `P01`..`P04`, including an
  operator confirmation that no consent dialog appeared). A record stops
  authorising migration as soon as the registered task definition changes.
- **`tests/test_secure_apps_privtask.py`**: task definition canonicalization
  and fingerprint, XML round-trip, tamper detection (wrong action, wrong run
  level, wrong principal, unexpected arguments, extra action, trigger added,
  disabled task, missing task, changed helper/interpreter digest), peer
  identity (high helper accepted, medium/claimed-elevated/foreign-image/other
  account/other session/impostor rejected), the helper's own client rule
  loaded out of the shipped PowerShell (medium broker accepted, elevated
  broker rejected, unmeasurable client rejected), silent start and start
  timeout, lease and idle behaviour, restart after idle exit, and the broker
  paths: unlock and lock start the helper silently, Default and Aggressive
  reopen after the helper exited, and a lock that cannot start it never
  reports LOCKED.

- **Reliable taskbar edge reveal (`Scripts/taskbar_edge/TaskbarEdge.exe`).**
  With taskbar auto-hide on, pushing the pointer against a monitor's bottom
  edge now reveals that monitor's taskbar even when another desktop window owns
  or covers the activation pixels. A dedicated non-elevated per-user helper does
  the work: Per-Monitor DPI Awareness V2, `GetCursorPos` + `MonitorFromPoint`
  (never `WindowFromPoint`, the foreground window or `WM_MOUSEMOVE` delivery), a
  bounded 30 ms poll instead of a `WH_MOUSE_LL` hook, a two-sample / 30 ms dwell
  against the last 2 px of the monitor's *full* rectangle, and per-monitor bar
  discovery through `SHAppBarMessage(ABM_GETAUTOHIDEBAREX, ABE_BOTTOM)` with a
  `Shell_TrayWnd` / `Shell_SecondaryTrayWnd` fallback mapped back by
  `MonitorFromWindow`. Signed coordinates throughout, so monitors left of or
  above the primary display behave identically. The reveal is one
  `SetWindowPos(HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE |
  SWP_SHOWWINDOW | SWP_FRAMECHANGED)`: it never moves, resizes, activates or
  focuses the bar, never touches `StuckRects3`, never toggles auto-hide and
  never restarts Explorer — Windows keeps owning the hide. Measured on the
  target Windows 10 shell: 20/20 reveals on each of three monitors, foreground
  HWND preserved, bar geometry unchanged, native hide intact, 0.0 % idle CPU.
  One instance per session (`Local\SaitulsTaskbarEdge`), `--status`,
  `--self-test` and `--stop` diagnostics, taskbar handles revalidated every
  sample so an Explorer restart recovers on its own.
- **SAITULS Settings → Taskbar.** Two switches — `Reliable taskbar edge reveal`
  and `Start with SAITULS / Windows` — plus a live helper status line. The
  feature is off until the user enables it once; upgrading SAITULS never turns
  it on. `SAITULS.cs` owns lifecycle only (start, stop, status, toggle) and
  contains no cursor polling, taskbar Win32 or shell-window manipulation, which
  `tests/test_taskbar_edge_contract.ps1` enforces. The helper's lifetime is
  independent of the SAITULS window: hiding to the tray or exiting SAITULS
  leaves it running, and the Settings switch is the explicit stop.

## Fixed

- **Secure Apps: the FIDO2 provider is written against python-fido2 2.x.**
  The shipped `yubikey-fido2-hmac-secret` provider was still constructing
  `Fido2Client(device, origin)` and relying on the implicit 1.x extension
  surface, which does not exist in the supported library. It now uses
  `DefaultClientDataCollector`, an explicit `UserInteraction` and
  `HmacSecretExtension(allow_hmac_secret=True)`, prefers
  `WindowsClient(..., allow_hmac_secret=True)` on Windows because the broker
  deliberately runs non-elevated, and reads extension results and credential
  ids off the real 2.x response objects. The supported range is pinned
  (`fido2>=2.0,<3`) and an install outside it is refused by name at load
  time. Capability reporting now says which path is actually usable --
  `WINDOWS_WEBAUTHN`, `DIRECT_CTAP` or `UNAVAILABLE` -- and a `WindowsClient`
  failure is never silently retried over an incompatible HID path.
  **Windows 10 22H2 ships `webauthn.dll` API version 2**, which cannot carry
  an hmac-secret salt (version 6 is the first that can), so that host is
  reported unusable instead of failing halfway through an enrollment.
- **Secure Apps: the recovery-acknowledgement deadlock is gone.**
  `BrokerWorker._ask_recovery_ack()` used to block the worker thread in a
  sleep loop waiting for `acknowledgeRecovery()` -- a queued slot on the same
  worker object, which therefore could not run until the thread stopped
  waiting for it. Enrollment is now an explicit state machine in `sa_enroll`
  (`ENROLL_PREPARE` -> `ENROLL_AWAITING_RECOVERY_ACK` ->
  `ENROLL_RECOVERY_ACCEPT` / `ENROLL_RECOVERY_DECLINE` -> `ENROLLMENT_READY`
  or rollback). The pending context holds the volume secret in a zeroizing
  `SecretBuffer` with a bounded timeout and explicit cancellation, and
  zeroizes plus rolls the container back on decline, timeout, cancellation
  and GUI shutdown. No `processEvents()` anywhere.
- **Secure Apps: first-run enrollment no longer needs the plaintext vault's
  path.** Enrollment used to create and mount the container at the profile's
  final `mount_path`, which on a first run is the directory holding the
  user's plaintext vault -- and Windows attaches a volume only to an empty
  directory, while migration refused to start until the container existed.
  Neither step could go first. Enrollment now builds the container at
  `<container dir>\_enrollment_mount`, unmounts it when the first key is
  enrolled, and never touches the real path; migration authenticates through
  the new `SecureBroker.authenticate_only()` (session without a mount) and
  keeps its own staging mount until the verified cutover. Every mount target
  is proven empty *before* any mutation starts, in both the Python backend
  and the privileged helper, rather than being discovered by
  `Add-PartitionAccessPath` after an image has been built, attached,
  formatted and encrypted.
- **Secure Apps: three storage-helper defects that broke every real first
  run.** `Invoke-Create` sent `detach vdisk` for a virtual disk that
  `create vdisk` never attached, so diskpart returned a failure and a
  perfectly good image became a refused enrollment. Every helper operation
  declared its payload parameter with the name of PowerShell's automatic
  unbound-argument variable, so it was shadowed and every request field read
  as empty. `Add-BitLockerKeyProtector` printed the recovery password in full
  on the warning stream. All three are fixed, and a real disposable Windows
  test now exercises the whole path.
- **Secure Apps: migration ignores volume-owned directories.** A real NTFS
  volume always carries `System Volume Information` (and friends), so the
  both-ways tree comparison failed on every genuine migration and the
  directories are not even readable to walk into. They are now excluded at
  the root of a tree only -- a note with that name three folders deep is the
  user's data and is copied and verified like anything else.
- **Secure Apps: closing the window is a secure-shutdown barrier.**
  `closeEvent()` used to emit a queued shutdown request and call
  `thread.quit()` in the next statement, racing process termination against
  the broker. The window now stays open until the worker reports that every
  protected application exited, every mounted volume detached, every session
  zeroized and the helper stopped -- or until secure shutdown fails, in which
  case exit is blocked and the window reports `RECOVERY_REQUIRED`. A
  watchdog turns a stuck shutdown into that same blocked, reported state
  instead of a silent exit.

## Security

- **Secure Apps: the protected-vault profile requires user verification.**
  `authentication.user_verification` (`required` / `preferred` /
  `discouraged`) defaults to `required`, and the reference Obsidian profile
  sets it explicitly. Enrollment refuses when the authenticator has no FIDO2
  PIN and no built-in verification, and every assertion is checked for the UV
  bit in the authenticator data, so possession of the key alone no longer
  opens the vault. The PIN is never stored, never logged and never crosses a
  command line, an environment variable or a temp file; on Windows the
  platform WebAuthn dialog collects it and it never enters the broker
  process.
- **Secure Apps: the privileged helper is authenticated by the kernel, not by
  its own claims.** The pipe now carries an explicit DACL (this user,
  Administrators, SYSTEM). The connected client is resolved with
  `GetNamedPipeClientProcessId`, its image path is pinned to the Windows
  PowerShell host, and its `TokenElevation` and integrity level are read from
  its token; the greeting's `pid` and `elevated` are cross-checked against
  those measurements. The random token is documented as a correlation nonce
  rather than an identity, because it is visible in the helper's command
  line. No volume secret is transmitted until verification completes, and
  `call()` fails closed if it somehow has not.
- **Secure Apps: CI installs the real python-fido2.** It used to be omitted
  on purpose, which meant the only line of the authentication provider CI
  ever executed was its `ImportError` handler. A new dependency-contract
  suite constructs the whole 2.x client surface against a stub CTAP2 device,
  with no authenticator, no elevation and no touch.

## Added

- **Secure Apps: a universal FIDO2-gated launcher for protected
  applications.** New subsystem under `Scripts\secure_apps\`, reached from a
  single `Secure Apps` button in the SAITULS Tools tab. Obsidian is the
  reference profile; the architecture is profile-driven, so further protected
  applications, storage targets, authentication providers, lock conditions
  and launch policies are added through `secure_apps.json` rather than new
  code. `SAITULS.cs` gained the button and one status line read from the
  subsystem's non-secret state file, and nothing else: no FIDO2 protocol
  code, no key handling, no BitLocker lifecycle, no idle monitoring and no
  protected-process management live in the shell, exactly as with SAIPATCH
  and Scenarios.

  The design separates two clocks that are usually conflated. In the default
  policy the **authentication session** survives six idle hours, but the
  **vault is detached the moment the protected application exits**; reopening
  inside the session remounts without another key touch, and the data was not
  sitting readable in between. "Idle" means six hours without *protected*
  activity -- a protected launch, the protected application owning the
  foreground window, or an explicit Secure Apps action -- not six hours
  without mouse movement somewhere else on Windows. Aggressive mode
  invalidates the session the instant the application exits.

  Key hierarchy: FIDO2 `hmac-secret` output -> HKDF-SHA256 with
  profile-specific context -> AES-256-GCM unwrap of a random BitLocker volume
  secret, with the wrapped-key AAD bound to profile id, container id,
  credential id and schema version. Only ciphertext and public metadata reach
  disk; the hmac-secret output, the derived KEK and the volume secret live in
  zeroizing buffers and never leave broker memory. Enrollment **refuses** an
  authenticator without the CTAP2 `hmac-secret` extension instead of silently
  downgrading an encrypted vault to a presence check, and because the volume
  secret is wrapped rather than derived, a second security key can be added
  without re-encrypting the vault. The BitLocker recovery material is shown
  exactly once, requires a typed acknowledgement, and is written nowhere --
  least of all beside the container it opens.

  Storage is a `bitlocker-vhdx` backend behind an `ISecureStorageBackend`
  abstraction: a BitLocker XTS-AES-256 NTFS volume inside a data VHDX,
  mounted onto a directory mount point so the vault keeps its historical
  path. BitLocker addresses the volume by its `\\?\Volume{GUID}\` path, so
  the vault is never exposed at a drive letter just to be unlocked. The
  unlock secret never appears on a command line, in an environment variable,
  in a temp file, in a window title or in any log: the medium-integrity
  broker hands it to an elevated helper (`sa_storage_helper.ps1`) over a
  single-instance named pipe, and the helper turns it into a SecureString for
  the in-process BitLocker call. The broker stays non-elevated so the
  protected application does too.

  The secure-lock sequence stops accepting launches, requests a graceful
  close, waits a bounded time, optionally terminates, **confirms the whole
  process tree exited**, flushes, unmounts and zeroizes -- in that order.
  Storage is never detached while a process in the tree is alive, because
  Obsidian is Electron and the parent can exit while a renderer still holds a
  handle on the vault. Workstation lock, logoff, suspend, shutdown, broker
  shutdown and manual Lock Now all route through the same sequence under
  per-profile policy flags.

  Crash recovery is pessimistic on purpose: a crashed broker does not imply a
  locked vault. A mounted volume with no live owner, a recorded exposure with
  no live owner, an `attached_locked` container and an unreadable state file
  all surface `RECOVERY_REQUIRED` and refuse new launches until a safe relock
  completes.

- **Plaintext vault migration that cannot lose the original.**
  `sa_migrate.py` runs PRECHECK -> CREATE -> COPY -> VERIFY_TREE ->
  VERIFY_SIZES -> VERIFY_HASHES -> VERIFY_APP -> CLOSE_APP -> RELOCK ->
  REMOUNT -> REVERIFY -> CUTOVER against a staging mount, so the plaintext
  tree owns its real path until the very last step. Note content is never
  inspected, parsed, normalised or rewritten -- files are copied byte for
  byte through the `\\?\` long-path namespace, with no ZIP/7z repack
  anywhere. Cutover renames the plaintext aside rather than deleting it, and
  the result is reported as two facts, `MIGRATION_VERIFIED` **and**
  `PLAINTEXT_SOURCE_REMAINS`, until the user removes the old copy
  themselves: a gate that claims a vault is protected while a readable copy
  sits next to it is telling the user something untrue. A journal makes an
  interruption before the cutover resumable, and an interruption *inside* the
  cutover repairable in both directions.

- **AI consoles: a data-driven, non-elevated console launcher.** New
  `Scripts\consoles\` subsystem with `claude-1`, `claude-2` and
  `antigravity` profiles, reached from an `AI Consoles` button in the Tools
  tab. The two Claude profiles resolve the same `claude` command and differ
  only in the child `CLAUDE_CONFIG_DIR`; the launcher never modifies the
  SAITULS process environment and refuses `HOME`, `USERPROFILE`, `PATH` and
  eight other keys outright. Account isolation is documented for what it
  actually is -- upstream Claude Code may still discover user-level
  instructions from outside `CLAUDE_CONFIG_DIR`, so these are isolated at
  launcher level, not sandboxed.

  These consoles deliberately do **not** inherit the Explorer-menu
  OpenCode/Cline launcher's maximum-privilege elevation: there is no
  `-Verb RunAs` anywhere in `CONSOLES.ps1`, and a future profile declaring
  `requires_admin` causes a refusal rather than a self-elevation. The command
  is resolved on PATH before launch so a missing CLI produces a dependency
  error instead of a window that flashes and disappears; the title is
  project-first under the 255-character OSC ceiling; Ctrl+C and Ctrl+V keep
  working; a nonzero exit keeps the window open.

- **154 new hermetic tests.** `tests/test_secure_apps.py` (90) drives the
  whole lifecycle against a fake authentication provider, a fake storage
  backend, an injected clock and an injected process adapter -- six-hour idle
  expiry included -- covering schema validation, unknown-profile rejection,
  path canonicalization and reparse-point refusal, capability failure, wrong
  and cancelled FIDO2 operations, tampered ciphertext, wrong profile AAD,
  unlock/mount/launch failures, default reopen inside and after the session,
  aggressive reopen, idle expiry with the application still open, Lock Now,
  workstation-lock and suspend transitions, broker restart over an
  unexpectedly mounted volume, the double-launch race, two concurrent unlock
  requests, and all three migration interruption windows.
  `tests/test_secure_apps_hygiene.py` (28) pushes a canary secret through a
  full lifecycle and then hunts for it in every written byte, every process
  argument list and the environment, and statically refuses the source shapes
  that make a leak possible. `tests/test_consoles_launcher.py` (36) covers
  the console registry, both Claude environments, environment isolation,
  Antigravity resolution and the elevation boundary. CI runs all of them and
  requires no security key; hardware coverage lives in the explicitly
  interactive `tests/secure_apps_interactive.py`, which CI never runs.

## 0.2.0 (2026-09-06)

## Added

- **`DEL JUNK --all-disks` now streams drive by drive.** The default ALL DISKS
  run no longer scans the whole machine, shows one combined preview and waits for
  a single final confirmation before anything is deleted: each eligible local
  fixed volume is scanned, preflighted and deleted immediately, then the worker
  moves to the next drive, so reclaimed bytes grow while later drives are still
  being scanned. Every decision is journalled to a durable append-only
  `stream-*.journal.jsonl` under `%LOCALAPPDATA%\SAITULS\DEL_JUNK` (`INTENT`
  before any deletion, then `DELETED` / `SKIPPED_CHANGED` / `RISKY_EXCLUDED` /
  `FAILED` / `CANCELLED`, each with timestamp, drive, path, classification,
  bytes and error), so a crash or cancel leaves truthful partial evidence
  without claiming rollback for permanently deleted junk. The historical
  scan-all-then-confirm-once flow stays reachable behind the new `--preview`
  flag. `tests/test_del_junk_all_disks.py` gains a streaming case proving drive
  1 is fully deleted before drive 2's scan begins.

- **One cross-process registry mutation lock for every SAITULS writer.**
  `INSTALL_ALL.PS1`, `INSTALL_AI_AGENT_MENUS.PS1`, `Add-CodexContextMenus.ps1`,
  `Remove-CodexContextMenus.ps1`, `Registry/IMPORT_SAFE.PS1`, `setup.ps1`'s
  autostart Run-key write and both `SAITULS.cs` Run-key writers now share the
  single named mutex `Global\SAITULS_REGISTRY_MUTATION`: a second writer exits
  with a clear BUSY result and performs zero mutation. `INSTALL_GUI.PS1` no
  longer holds the lock for the lifetime of its form - each Install/Uninstall
  click takes it for exactly one bounded transaction and releases it in
  `finally`, with the mutation buttons disabled while an operation runs, so a
  user idling in the GUI blocks nobody. Lock ordering is fixed: `setup.ps1`
  keeps its outer `Global\SAITULS_SETUP` whole-run lock and takes the registry
  lock inside it, never the reverse. `tests/test_registry_lock.ps1` holds the
  lock and launches every writer, then runs two real Codex-menu transactions
  concurrently and proves the result is exactly one complete generation, never an
  interleaved mix.

- **Media payload validity and atomic publication in `setup.ps1`.** Presence is
  no longer validity: `Scripts/media_payload.ps1` adds `Test-ExecutableValid`
  (a meaningful minimum size per tool, the PE `MZ` header and `PE\0\0`
  signature, plus a best-effort `--version` probe) and `Publish-Pair`. Every
  payload is downloaded/extracted into a unique staging directory and validated
  before any destination is touched, and the coupled `ffmpeg.exe`/`ffprobe.exe`
  pair is published as ONE generation with rollback: a failure while publishing
  the second member restores the previous pair byte-intact, so a truncated
  download can never leave a mixed pair. `yt-dlp.exe`, `aria2c.exe` and
  `deno.exe` follow the same stage-validate-publish flow. `setup.ps1
  -SkipPayload` still exits 0. `tests/test_media_payload.ps1` seeds a zero-byte
  `ffmpeg.exe`, a truncated `ffprobe.exe` and a text file renamed `yt-dlp.exe`,
  refuses all three, and injects a member-2 publication failure to prove the old
  pair survives.

- **Transactional SAISPIN scheduled-task replacement with exact cadence
  verification.** `Scripts/Install-SaispinTask.ps1` captures the complete prior
  task definition (action, triggers, principal, settings, description) before
  the destructive unregister and restores it if the replacement or its
  post-registration verification fails; with no prior task a failed
  registration leaves nothing half-created. `Assert-SaispinTask` parses the
  Task-Scheduler ISO-8601 repetition interval (`PT5M`) through
  `[System.Xml.XmlConvert]::ToTimeSpan` and compares it exactly with the
  requested cadence, printing both `expected 00:05:00` and `observed 00:15:00`
  on drift instead of accepting any existing interval.
  `tests/test_saispin_task.ps1` runs against the real Windows Task Scheduler in
  a disposable `\T135TEST\` namespace: a 5->15-minute tamper must FAIL, and a
  forced replacement failure must leave the original task restored.

## Fixed

- **`INSTALL_ALL.PS1` locale switch is a real registry transaction.** Every
  REG source, launcher, CLI and destination is resolved before the first
  mutation (a deleted source REG used to drop silently out of the uninstall
  list and strand its keys), the managed registry state plus the exact
  `.installed_lang` marker bytes are snapshotted before any change, and any
  import or marker-write failure rolls the managed keys AND the marker back to
  the pre-run bytes, exits nonzero and names the failing component - the old
  code printed "Done!" with a half-applied locale generation. Removed subkeys
  are recreated, keys created only by the failed run are removed, registry
  value kinds (Binary/MultiString/ExpandString/QWord) are preserved exactly, and
  an unrelated sibling key is untouched. `tests/test_installer_registry.ps1`
  proves this against a real disposable HKCU branch with real `reg.exe`
  operations.

- **`setup.ps1 -EnableAutostart` Run-key write no longer races other registry
  writers** (see the shared mutation lock above), and the durability layer of
  the SAISPIN watcher (`Save-JsonAtomic`, durable PRE-KILL audit, rename-based
  bounded log rotation) is now covered by the watcher `-SelfTest` on BOTH
  PowerShell 5.1 and pwsh, with zero termination whenever durable state or the
  pre-kill audit is unavailable.



## Added

- **`DEL JUNK ALL DISKS AVAILABLE` cleans every local disk in one pass.** The
  new Explorer verb enumerates local fixed volumes dynamically — the system
  drive included, network, mapped-offline, removable, optical, RAM and unmounted
  drives excluded with a stated reason — and scans them all under `SAFE_DISK`.
  `AGGRESSIVE_PROJECT` is unreachable from this path. The user gets one combined
  preview (per-drive candidates, total reclaimable bytes, excluded/risky counts,
  skipped drives) and one confirmation; deletion then runs drive by drive from
  per-drive byte manifests plus a master manifest and log. Each drive holds its
  own single-flight reservation, so a drive already being cleaned — including by
  a folder cleanup anywhere inside it — is skipped instead of racing. A drive
  that vanishes or fails never stops the others, cancellation reports what was
  actually deleted instead of `CLEANED`, and a junk-named directory containing a
  protected file survives whole as `risky_excluded`. Deletion is offered only
  when the audited safety repairs still hold: `ALL_DISKS_DESTRUCTIVE_READY =
  protected-tree test PASS AND overlap-lock test PASS AND SAFE_DISK regression
  suite PASS`; a failed gate downgrades the run to scan-and-report.
  `tests/test_del_junk_all_disks.py` proves the whole contract, gate included.

## Fixed

- **The locale generator no longer publishes a half-translated menu.**
  `i18n/tools/gen_locale_reg.py` treated a missing label or a missing bundle key
  as a line in its report and wrote the file anyway, so a renamed menu entry or a
  pruned string key produced a generation with some keys translated and some
  left in Russian - and because each file was written straight into the
  destination, a crash midway left exactly that mixture behind for
  `INSTALL_ALL.PS1 -Lang` to import. Every mandatory substitution is now
  enforced: an install `.reg` must carry all the labels its `reg-map.json` entry
  claims, a removal counterpart must carry either its parent's whole localized
  set or none of it, and any violation exits nonzero having written nothing. A
  successful run renders the whole locale in memory, stages it in a fresh
  directory on the destination volume and swaps it in by rename, so the
  destination is either the previous generation or the new one. The documented
  `--out PATH` is now a real option parsed with `argparse` - it used to be read
  as a bare positional, so `--out i18n/reg/et` created a directory literally
  named `--out` and silently discarded the path - and an unrecognized argument
  exits nonzero instead of being ignored. Presence is judged against the
  original text, so a label that exists only nested inside a longer one
  (`Сжать` inside `Сжать в MP4`) is not mistaken for drift once the longer
  label has consumed it, while the report counts the substitutions actually
  performed. Regenerating `i18n/reg/et` produces byte-identical output.
  `tests/test_locale_generation.ps1` covers all of it against a throwaway
  project tree.

- **A cancelled UAC prompt no longer looks like a finished install.**
  `Installers/INSTALL_GUI.PS1` and `Remove-CodexContextMenus.ps1` started their
  elevated copy and exited immediately, so the caller was told the launch had
  succeeded rather than the registry operation: the parent returned 0 while the
  elevated child was still importing, and a dismissed prompt was
  indistinguishable from a completed run. Both now wait for that child and exit
  with its actual code, a cancelled or failed elevation exits nonzero with
  `Nothing was changed`, and the terminal success line is printed only by the
  elevated child after the mutation. `INSTALL_GUI.PS1` also returns the number of
  operations that failed or were skipped for a missing `.reg`, so a partly
  applied batch is reported as such.
  `tests/test_self_elevation_result.ps1` drives both scripts through a stubbed
  elevation that returns 0, 1 and 7, returns nothing, and throws
  `ERROR_CANCELLED`.

- **SAISPIN stops repeating the same balloon every five minutes, and stops paying
  for the sweep twice.** A sweep runs every five minutes and a runaway can spin
  for hours, so one detection used to mean a fresh tray notification twelve times
  an hour for the same process — each costing 1.2 seconds of sweep time in the
  notification pump, once per alerting process. Notifications are now suppressed
  per process identity, PID plus start time, for an hour, tracked in an
  atomically written `saispin_notify.json` beside the state file, and several
  processes crossing the threshold in one sweep now share a single balloon that
  names the count and the mode. A changed verdict always breaks through
  immediately, so an alert that becomes a kill, or a refused kill that becomes a
  real one, is never held back; so does a cold-to-hot recurrence and a recycled
  PID, because the identity key carries the start time. Suppression touches the
  balloon only: `saispin.log` still records every sweep and now carries
  `NOTIFIED=0|1` per decision. The ledger is rebuilt from the identities seen in
  each sweep, so dead processes drop out on their own; a missing or corrupt ledger
  costs one duplicate balloon and can never influence a kill. Sample construction
  was also lifted out of the sweep into one pure function that appends to a list
  instead of rebuilding the array per process, removing an N-squared copy that
  grew with the size of the machine rather than with the problem — measured at
  2.2ms versus 1.0ms for 600 records and 106ms versus 48ms for 10000.
  `saispin_watch.ps1 -SelfTest` covers the record shape at 10, 100, 1000 and
  10000 processes, the append shape itself, the engine accepting exactly the
  collection the sweep builds, twelve consecutive unchanged sweeps producing
  twelve log lines and one balloon, the escalation break-through, the batch body
  budget, and the unreadable stamp, which resolves toward notifying rather than
  silence.

- **The SAITULS window stops churning paint resources.** It keeps one pixel
  font per point size and one centered text format for its whole lifetime and
  releases them on dispose, instead of building a font per text, button and tab
  measurement on every repaint and leaving the measurement font plus a text
  format per button to the finalizer. Font metrics, anti-aliasing and the tab
  and button geometry are unchanged.

- **Problip stops churning paint resources.** The window keeps one pixel font
  per size and one centered text format for its whole lifetime and releases
  them on dispose, instead of building around twenty fonts per repaint and
  leaving one measurement font plus a text format per button to the finalizer.
  Dragging the volume slider now invalidates the window so mouse moves coalesce
  into one repaint rather than forcing a synchronous full paint per event. The
  rendering, geometry and saved volume are unchanged, and the native font
  handle release stays in place.

- **`NEW PROJ` reports success only for a complete skeleton.** The command
  preflights `_new_project` and all four required folders, refuses to replace
  anything that is not a folder already sitting on one of those names, and
  verifies `ae`, `c4d`, `_output` and `_input` before it returns success.
  Previously only `_output` was checked, so a blocked folder could leave a
  partial skeleton behind a zero exit code. A failed run now removes only the
  still-empty folders it created itself and preserves every pre-existing item.

- **Second launches no longer disappear during SAITULS startup.** The named
  activation event is now created before single-instance mutex ownership is
  published. A secondary always signals that shared event, and its AutoReset
  state remains latched until the primary finishes startup and registers the
  UI callback.

- **Legacy `ClineHere` registry ownership is deterministic.** The Cline
  installer recognizes the exact historical SAITULS machine-wide signature
  and removes only those project-owned keys during migration or uninstall;
  foreign same-name keys survive. The Codex remover no longer reads or deletes
  any Cline registry state.

- **Release payload builds are transactional.** `BUILD_PAYLOAD.cmd` now reads
  `VERSION`, preflights the explicit `PAYLOAD_MANIFEST.txt`, writes to a unique
  staging ZIP, checks the archiver exit code and exact archive members, then
  publishes the validated artifact. Missing inputs, partial archives, and bad
  member sets fail without deleting or overwriting the prior release ZIP.

- **`DEL JUNK` now has a conservative whole-drive policy.** Drive and mounted
  volume roots select `SAFE_DISK`, which prunes repository/system/reparse
  boundaries, protects ambiguous build/log/cache/temp/dependency subtrees,
  age-gates partial downloads, and keeps diagnostic and user-ambiguous file
  types. Explicit project folders retain the historical aggressive policy.
  Large scans expose progress and cancellation; deletion requires a durable
  unique byte manifest, revalidates every candidate against that manifest, and
  holds an OS-managed per-target lock. The permanent no-Recycle-Bin behavior is
  stated in the confirmation dialog.

- **The five Python Explorer commands never ran at all.** `DEL JUNK`, `DEL DUP`,
  `DEL EMPTY`, `DEL SAME` and `PACK` were registered as `pythonw.exe "<script>"
  "%1"`, and a shell verb resolves a bare executable name only from the Windows
  directory and `System32` — never from `PATH`, where a per-user Python install
  lives. Right-clicking produced `Application not found` and nothing else. All
  ten `.REG` files (including the Estonian locale tree) now invoke `pyw.exe`, the
  launcher Python installs into the Windows directory, which starts the same
  interpreter; `setup.ps1` counts it as part of a working Python and passes
  `Include_launcher=1`.
- **A drive root reached those workers as the wrong directory.** Explorer
  substitutes `"%1"` literally, so on `G:\` the trailing backslash escapes the
  closing quote and the worker receives `G:"`. Stripping the quote leaves `G:`,
  which is *drive-relative*: its absolute path is whatever that process's saved
  directory for `G:` happens to be, not the root. New `Scripts/shell_arg.py`
  owns the one rule and every worker takes its target through it; `PACK` also
  refuses a drive root outright rather than moving a whole volume into a
  subfolder of itself.
- **`IMPORT_SAFE.PS1` reported success while writing nothing.** It substituted
  the toolkit root into the `.reg` text as a raw path, but inside quoted registry
  value data a backslash is an escape character — so every command line became
  unparseable. `reg.exe` skips a value line it cannot parse *and still exits 0*,
  so the wrapper printed `Imported OK`, wrote no command value, and left whatever
  an earlier install had put there. This is why corrected `.REG` files kept
  appearing to install while the menus stayed broken, and it silently affected
  the GUI's Menus tab, which imports through this wrapper. The root is now
  escaped the way `INSTALL_ALL.PS1` already did it, and every intended value is
  read back after the import instead of trusting the exit code.

- **Deleting duplicates can no longer destroy a file that stopped being a
  duplicate.** `DEL_DUP.PYW` hashed every candidate, then waited an unbounded
  amount of time for the confirmation dialog, then deleted by path without
  looking at the bytes again — so a file edited or replaced during that pause was
  still removed, and this worker bypasses the Recycle Bin. The qualifying
  SHA-256 and the copy being kept are now carried into the deletion phase and
  both are re-hashed immediately before `os.remove`; anything that changed or
  vanished is skipped and named in the summary. `tests/test_workers.py` proves
  it by mutating the file from inside the confirmation hook, and fails against
  the previous implementation.
- **Merging audio no longer overwrites an unrelated neighbouring file.**
  `MERGE_AUD.CMD` pointed `ffmpeg -y` at the predictable sibling
  `<name>_merged<ext>`, so running it on `foo.mkv` silently destroyed an existing
  `foo_merged.mkv` before the intended replacement happened. Scratch is now
  `_merge_<GUID>` in the same directory, the exit code *and* the output's
  existence are checked before the original is replaced, and a failed
  replacement says where the result was left instead of losing both.
- **A failed registry import is no longer reported as success.**
  `INSTALL_ALL.PS1` and `INSTALL_GUI.PS1` ran `reg.exe` through `Start-Process`
  without `-PassThru` and never read the exit code, so a denied or malformed
  import still ended with `Done!` or "Successfully installed". Both now throw on
  a nonzero exit; `INSTALL_ALL` collects every failure, prints them and exits 1;
  the GUI preflights the selected files and reports success only when every
  requested import completed. Self-elevation also uses `-Wait -PassThru` and
  exits with the elevated child's code, so `setup.ps1` can finally see the
  result — it now checks that code instead of `$?`.
- **Autostart has one owner again.** The INI is authoritative and the HKCU `Run`
  value is its projection, reapplied on every launch — but `setup.ps1
  -EnableAutostart` wrote only the registry, so it enabled autostart until the
  very next SAITULS start, which read `AutoStart=0` and deleted the entry.
  Problip's own toggle had the mirror-image bug. Both now write the INI first.
- **`DL_YT.CMD` no longer shares one clipboard file between concurrent runs.**
  Every invocation staged its URLs in the same `%TEMP%\yt-dlp-urls.txt`, so a
  second launch could overwrite the first one's links between capture and
  download — and Explorer starts these independently. Each run now owns a
  GUID-named list, aborts if the clipboard capture produced nothing, and deletes
  the file through one cleanup path reached by every branch.
- **`DEL_EMPTY.PYW` reports a directory it cannot read instead of dying.** The
  per-directory failure list was there, but the `os.listdir` that actually fails
  sat outside the `try`, so an access-denied directory killed the process after
  earlier directories had already been removed — no log, no summary, the
  operation just vanished mid-run.
- **"Install / repair" now repairs a broken binary, and two clicks cannot race
  each other.** `setup.ps1` decided the app was fine whenever `SAITULS.exe`
  merely existed, so a zero-byte or truncated build was reported as ready
  forever. Validity is now a real rule (missing, under 4 KB, no `MZ`/PE
  signature, unreadable, or older than its own source), the rebuild compiles to
  a per-run staging path and is re-validated before the swap, and a target that
  is the running image is refused with the PIDs named and the build parked as
  `SAITULS.exe.new` instead of half-writing a live executable. A
  `Global\SAITULS_SETUP` mutex holds the whole mutating run, so a second
  concurrent setup reports and exits instead of deleting the first one's
  downloads — every temporary path now derives from a per-run token.
- **The Menus tab tells you what actually happened.** A selected feature whose
  `.REG` was missing was silently skipped, and the elevated PowerShell child's
  exit code was thrown away, so a rejected registry import was indistinguishable
  from success once the console window closed. Both install and uninstall now
  preflight the whole selection and import nothing if a file is missing; each
  import is individually guarded, the child exits with the number of failures,
  and SAITULS reports that count on the UI thread.
- **Switching menu language no longer leaves two translated menu trees.**
  `INSTALL_ALL.PS1 -Lang` only changed which directory the `.REG` files came
  from, so installing English and then Estonian left both key sets in the
  registry, and `-Uninstall` without `-Lang` removed the English removal set
  while the localized generation stayed orphaned. The applied generation is now
  recorded in `Installers\.installed_lang`: a language switch removes the
  recorded generation before applying the new one, an uninstall without `-Lang`
  removes what is actually installed,   and the marker is only written after a
  run with zero failures. `tests/test_locale_generation.ps1` proves the order
  and the marker against the real installer.
- **A wrong `-LauncherRoot` no longer destroys a working Codex menu.**
  `Add-CodexContextMenus.ps1` deleted the existing `CodexHere` subtree and only
  then checked each launcher path from inside the same write loop, so pointing it
  at the wrong directory left `Directory` wiped and `Directory\Background`
  half-written — a working cascade replaced by an empty parent key. All three
  launchers are now validated before the first registry write; a missing one
  names every path it could not find and exits 1 having changed nothing.
  `tests/test_codex_menu_preflight.ps1` proves both shells stay
  value-equivalent across the refusal, using a throwaway `HKCU` key.
- **The Codex menu's two shell roots are now one transaction.** `Directory` and
  `Directory\Background` were written one after another, each deleted before
  being rewritten, so a registry failure part-way through left the first
  converted and the second deleted-and-half-written with no way back. Both
  subtrees are exported before anything is removed and restored on any failure,
  and the run exits non-zero. The same test file proves it with an injected
  failure on the second root.
- **`DEL_JUNK.PYW` no longer walks the inside of a folder it has already
  condemned.** The scan was bottom-up, which cannot prune, so every descendant of
  a doomed `node_modules` was still visited and each junk file inside it was
  listed separately — measured at 123 directories and 401 file entries for a
  single deletion target, and the confirmation dialog counted all of them. The
  walk is now top-down and prunes a junk directory in place, so a condemned
  subtree costs one entry. A junk file outside any junk directory is still found;
  `tests/test_workers.py` proves both halves.
- **Problip no longer says it is running when its sound cannot load.** A missing
  or corrupt `blip01.wav` collapsed to an internal null player with nothing
  recorded, while the window still drew `ON` — the beeper was a silent no-op
  indistinguishable from having been switched off. The load failure is now kept
  and named: the window shows `ERR` plus the reason, the tray tooltip says the
  sound could not be loaded, and `ON` refuses to claim it is running. The WAV is
  validated up front (`SoundPlayer.Load` accepts arbitrary bytes and only fails
  later, inside a per-tick catch), and pressing `ON` after fixing the file
  recovers without a restart. `tests/test_problip_sound.ps1` drives the real
  engine against a missing, a corrupt and the shipped asset.

- **CI ran nine of the twenty-one automated harnesses, and two of them could
  pass without testing anything.** `.github/workflows/test.yml` provisioned a
  checkout and Python and then ran a subset, so twelve harnesses — the Explorer
  launch contract, the registry-import writes, SAIPATCH, the locale generation,
  the agent-menu transaction, the Codex preflight, the port allocator, SAISPIN,
  the native queue oracle and the Problip sound state among them — were
  developer-machine-only, and any of them could regress without breaking a
  build. The workflow now runs all twenty-one, preceded by a new
  `tests/ci_fixtures.ps1` step that declares and provisions the external
  prerequisites a clean runner lacks (the `pyw.exe` launcher a shell verb needs,
  the agent CLI stand-ins, the OpenCode plugin/SDK declaration files the patch
  contract reads, `node`, and the .NET Framework compiler) instead of inheriting
  them from ambient state.

  Two oracles were repaired to make that meaningful. `tests/test_shell_menu.ps1`
  asserted the drive-root argument contract through `os.path.abspath`, which
  launders the exact defect it exists to catch: the naive fix yields the
  drive-relative `E:`, whose absolute path is the process's saved directory for
  that drive, so whenever that directory is the root the wrong answer is
  indistinguishable from the right one. It now asserts the normalizer's own
  output, and discovers a fixed non-system drive at runtime rather than
  hardcoding `G:\`, which need not exist. `tests/test_problip_sound.ps1` printed
  `SKIP` and exited 0 when `csc.exe` was absent — a green result for a subject it
  never compiled — and now fails, matching the two repaint harnesses.


## Removed

- **LIMISAW is now its own project.** The agent quota tray monitor, its vendored
  Python provider stack (`Scripts/limisaw_probe.py`, `Scripts/limisaw_limits/`),
  its Wintage palette files (`Themes/`), its alert sounds (`Sounds/`), its icon
  (`heh.ico`) and its eleven C# test harnesses all left this repository. SAITULS
  loses the `Codex Limits` tool button, the Home readiness row and the
  `LIMISAW.exe` launch branch; nothing else depended on any of it — `SAITULS.cs`
  and `problip/Problip.cs` each carry their own palette and their own icon.
  `tests/test_regs.py` keeps every check that is not about LIMISAW, including
  the shell contracts (signed `WM_NCHITTEST` decode, `HFONT` release,
  exact-size tray icons) that were found in LIMISAW and fixed in all three apps
  at once.

## Added

- **SAISPIN — orphan CPU watchdog (dry-run by default, SRC-005).** Watches for
  processes that lost their parent lifecycle and kept pointlessly burning one
  core. The pure decision logic lives in `Scripts/saispin_logic.py`
  (`decide(history, sample, auto_kill)` → `IGNORE`/`ALERT`/`KILL`) and is
  exercised in isolation by `tests/test_saispin.py` (the safety rules where a
  wrong answer is invisible until it kills the wrong process). The watcher
  (`Scripts/saispin_watch.ps1`) samples each process's accumulated CPU twice
  three seconds apart, persists a consecutive-hot streak keyed on `PID +
  StartTime`, detects an orphan by re-verifying the parent's identity across a
  PID reuse, downgrades any `KILL` to `ALERT` when the state file had to be
  rebuilt after a bad read, takes the Scheduled Task installer
  (`Scripts/Install-SaispinTask.ps1`) at logon every five minutes, and holds a
  `Global\SAITULS_SAISPIN_WATCH` mutex so Task Scheduler can never start a
  second watcher. `Orphan ≠ kill` and `Spin ≠ kill` are enforced: a kill needs
  orphan **and** sustained spin **and** auto-kill **and** a matched identity
  **and** a non-allowlisted image, and never from stale history.

# SAITULS 0.1.3 (2026-09-03)

## Fixed

- **The LIMISAW tray no longer freezes on a stale number.** `NotifyIcon.Text`
  rejects anything over 63 characters, and the tooltip is assigned *before* the
  icon, so one over-long tooltip aborted the whole tray update. The bars/grid
  tooltip had no length clamp at all and reached 67 characters as soon as both
  accounts read 100% and a refresh went stale. Every tooltip branch now leaves
  through one clamp.
- **Borderless windows drag on every monitor.** `WM_NCHITTEST` packs two
  *signed* 16-bit screen coordinates; a checked `IntPtr`-to-`int` conversion
  overflows for any point on a monitor above the primary, so the title strip
  threw `OverflowException` there and stopped dragging. SAITULS, LIMISAW and
  Problip now decode the full native value unchecked.
- **Problip stopped leaking GDI fonts.** `Font.FromHfont` does not take
  ownership of the handle, and `OnPaint` created roughly twenty per repaint —
  measured at 3003 leaked GDI objects per 3000 fonts, against a hard 10 000
  per-process limit. It now clones the managed font and deletes the `HFONT`,
  matching what SAITULS and LIMISAW already did.
- **Problip volume drag no longer restarts a stopped beeper**, and no longer
  abandons its temp WAV: each rebuild deletes the previous file and exit clears
  the last one (18 orphaned `problip_*.wav` files were sitting in `%TEMP%`).
- **The LIMISAW probe timeout is reachable.** A blocking
  `StandardOutput.ReadToEnd()` ran before `WaitForExit(20000)`, so a wedged
  probe hung the refresh worker forever and the tray never updated again.
  Output is now read asynchronously. The reset balloon is also marshalled to the
  UI thread instead of being raised from the worker.
- **Tray and window icons ask the shell for the size it wants.** Plain
  `new Icon(path)` returns the 32×32 frame (128×128 for `heh.ico`), which the
  shell then downscaled into a 16×16 slot — the exact blur `UI.md` forbids.
- LIMISAW's grid/bars tray fill allocated one undisposed `SolidBrush` per row.
- **Every gauge in LIMISAW is now proportional.** A reading the tray does not
  draw — past the `TrayMax` cap, hidden, or carried forward from an older fetch
  — was passed to the gauge as `available: false`, so it drew an *empty* bar
  whatever its real percent was: with the default cap of one, a hover panel of
  eight readings showed one honest bar and seven empty ones, each still printing
  its true number. Dimmed readings now fill their true amount in the muted
  colour. The bar's track also moved from the panel background to the dark
  border colour, because a track the same colour as the panel is only its bevel
  outline, which at 8px reads as a filled bar.

## Added

- **LIMISAW watches three vendors, not one.** Codex, Claude Code and
  Antigravity are all probed through their own CLIs (`codex app-server`,
  `claude -p "/usage"`, `agy -p "/usage" --output-format json`), with the
  status-line cache, local transcripts and the IDE refusal journal as
  fallbacks. Accounts and windows are **discovered**: cards, tray metrics and
  menu rows are built from the live snapshot, so a new login needs no code
  change. The provider stack is vendored under `Scripts\limisaw_limits\` so
  SAITULS still ships standalone.
- **Pool-aware gating replaces the weekly lock.** A spent long window zeroes the
  shorter ones *in its own quota pool only*. Antigravity bills Gemini models and
  Claude/GPT models separately, so an exhausted Claude weekly no longer fakes a
  dead Gemini 5-hour window; the card shows `locked by <window>` and the balloon
  says the refill is still locked.
- **Sixteen tray pictures instead of three.** Four layouts — one number, two
  stacked numbers (worst short over worst long), one bar per reading, one cell
  per reading in a 1x1/2x2/3x3 grid — times four fill granularities (halves,
  quarters, eighths, exact per-pixel).
- **Antigravity's Claude/GPT 5-hour limit is no longer missing.** Each of its two
  model pools has its own weekly *and* 5-hour window. The CLI marks a pool's
  5-hour bucket `disabled` while that pool's weekly limit is spent and still
  reports `remaining_fraction: 1` for it — trusting that showed "100% free" on a
  pool that refuses every request, and the previous fix (dropping the bucket)
  hid the limit entirely. It is now kept as a real window with no number of its
  own: gated to 0% when its pool's weekly window is spent, `--` when the pool
  still has quota.
- **Hovering the tray icon opens a themed panel instead of a text tooltip.** One
  row per reading with the same gauge and colours the window uses, grouped under
  one header per account, with readings outside the tray selection dimmed rather
  than dropped. The shell tooltip is a single 63-character line, which cannot
  carry ten readings across three vendors; it is left empty so only one thing
  appears over the icon, and the one-line fallback (with its clamp) is still
  used if the panel cannot be created.
- **The tray context menu is painted in the active theme.** A system-white menu
  over a themed icon looked like a different application had opened. Disabled
  rows there are status lines, not unavailable actions, so they stay readable.
- **A slow vendor CLI no longer reads as "unavailable".** `agy -p "/usage"`
  answers in about 3 seconds but was measured at 14+; the 12-second per-account
  budget turned that tail into a phantom `Antigravity UNAVAILABLE`. Budgets are
  now 26s per account and 50s per sweep, inside the app's 75s kill timer.
- **The user picks what the tray shows.** Ten-plus windows across three vendors
  cannot fit a 16-pixel icon, so the new `Tray` tab lists every discovered
  reading with its live value and lets you reorder it — **by dragging** a row or
  with the arrows — hide or show it, and cap how many reach the icon (1-9). A
  press only becomes a drag after a few pixels, so a click never reorders
  anything by accident, and an insertion line shows where the row will land.
  Rows past the cap are dimmed rather than removed, so the cap's effect is
  visible; a newly discovered reading is shown by default, because silently
  hiding a fresh login would look like the vendor failed. `lowest` and the
  bars/grid pictures all read that same selection, while a metric pinned by name
  is still resolved against the full list.
- **Nothing in the window is positioned by a hardcoded offset any more.** Every
  row measures its own label and buttons and lays them out from that; text that
  shares a row with a control stops at the control's edge; a button label steps
  its font down 10 → 9 → 8 before losing a character; the theme grid picks its
  column count from the widest name. This fixes `Showing: Left` rendering as
  `Showing: Le` and the limit bars sliding under the buttons on the `Tray` and
  `Settings` tabs.
- **Every setting is duplicated in the window.** Four tabs (`Accounts`, `Tray`,
  `Settings`, `CLIs`; keys `1`-`4`) carry layout, fill steps, refresh interval,
  Used/Left, reset balloons, autostart, all themes and a button that opens
  `LIMISAW.ini`. The tray menu keeps the same switches and now links to the
  tabs — reordering a list inside a context menu is what the tabs exist to
  avoid.
- **The window grows to fit its content.** The scrollbar is gone: the account
  list is short and fully known, so scrolling only hid rows behind an extra
  gesture. Height is clamped to the screen the window sits on.
- **Themes are Wintage palette files** (`Themes\*.json`, the same files the
  browser theme uses — 16 of them). Golden Default stays built in and is the
  fallback for a missing or partial file; `T` cycles, the tray menu pins one. A
  switch repoints the active palette instead of recolouring in place, so one
  repaint can never mix two themes.
- **LIMISAW Used/Left switch.** One window button (`Showing: Left` /
  `Showing: Used`), the `U` key and a tray-menu entry flip every percentage
  between remaining and spent — cards, tray number, tooltip and the account
  rows at once, **and the fill flips with them**: in Used mode a bar fills up as
  quota is spent, so an exhausted account reads as a full bar labelled `100%`
  instead of an empty one. The colour never inverts — it is always derived from
  what is *left* — so red keeps meaning "almost out" in both modes. `ShowUsed`
  in `LIMISAW.ini` remembers the choice, and readings are still stored and
  compared as remaining, so colour thresholds, the `lowest` metric and reset
  detection keep one meaning.
- **LIMISAW alerts are configurable, per event.** The `Settings` tab gained four
  rows: a volume for all alerts, a **reset** row (balloon and/or chime on a real
  refill), a **when left ≤ N%** row (threshold in 5-point steps, 5-95) and the
  low alert's sound. Each alert is *two* switches — the balloon and the sound —
  because muting one and keeping the other is a real preference, and each has a
  `WAV` picker and a `Play` preview. Nothing about *which* sound plays is
  hardcoded: the picker lists the WAVs in `Sounds\` (or a folder you point it at,
  `C:\Windows\Media` as the fallback) and any file outside that folder is stored
  by absolute path. Ships `success_powerup.wav` on reset and
  `pop_cartoon_pop.wav` on the low alert, both re-pickable.
  - Sound plays at the chosen volume by scaling the WAV's samples into a cached
    copy (`%TEMP%\limisaw_sounds`, quantised to 5% steps and pruned after 3 days),
    because `SoundPlayer` has no volume of its own — the same trick Problip's
    blip engine uses.
  - The low alert fires **once per window per reset cycle**: the first sweep after
    launch only records what is already low (no balloon storm), a window that
    stays low does not re-alert each refresh, and it re-arms when its own reset
    stamp changes or the number climbs back above the threshold plus a 10-point
    band. `NotifyLow` / `LowPct` / `ResetSound` / `ResetSoundFile` /
    `LowSoundFile` / `SoundVolume` / `SoundDir` in `LIMISAW.ini`.
- **The alert follow-ups (all four asked for after the first pass).**
  - **Volume and threshold are drag sliders**, not `-`/`+` steppers: the same
    12px rail + 10px knob Problip uses, press anywhere on the rail to jump,
    drag to scrub. Volume ships at **5%** now, not 70% — an alert chime at 70%
    on a quiet desk is a jump-scare.
  - **`Play` reflects the real volume.** It used to floor the preview at 25%,
    which made the button a liar exactly when the setting mattered most.
  - **A live tray preview on the `Settings` tab**, next to a three-way readout
    switch. It renders through the *same* `RenderTrayBitmap` the shell gets, so
    the preview cannot disagree with the icon.
  - **Countdown readout: `Tray shows: Off / % / Time`.** `Time` puts the time
    left until that window's own reset in the icon — `12m`, `3h`, `2d`, two
    characters at 8pt so it stays legible at 16px; three characters step down to
    6pt. A missing, unparseable or past reset stamp is `--`, never `0m`, because
    zero minutes reads as "right now" and would lie on a stale probe. `Off`
    draws no number at all (bar/cell layouts only). `TrayShow` in `LIMISAW.ini`.
  - The once-per-cycle rule now has a **replay regression test**: three
    identical sweeps over the same low window fire exactly one alert
    (`tests\notify_alerts.cs`), which is the "not every 3 minutes" complaint
    pinned down.
- **One-click CLI installers.** The `Install CLIs` panel shows each vendor's own
  published install command, its publisher and its target path. Piping a remote
  script into a shell stays explicit: nothing runs without a Yes, and the
  installer runs in a visible PowerShell window.
- **A single left click on the tray icon opens the window.** Double click still
  works; the icon is the app's main entry point and a double click is not
  discoverable.
- `tests\tray_tip.cs`: proves every tooltip branch is assignable to a real
  `NotifyIcon` across 0/1/3/5 accounts x Used/Left x stale (the tip is grown
  account by account, so more vendors can never overflow it), that the shell
  still rejects 64 characters (the clamp is load-bearing), and that
  `WM_NCHITTEST` decodes all four monitor quadrants. `tests\tray_items.cs`
  pins the tray-selection rules (saved order, default-visible new readings,
  hidden rows never drawn, cap applied last, stale ids skipped, moves clamped).
  `tests\carry_forward.cs` pins the stale-numbers rules, `tests\tray_popup.cs`
  the hover panel, `tests\layout_fit.cs` drives the real window through every
  tab x theme x account shape and fails on overlapping controls, covered
  content or a cropped label, and `tests\test_limits.py` pins Antigravity's
  per-pool windows including the disabled 5-hour bucket.
  `tests\pixel_purity.cs` now runs 5120 checks: every layout x fill step x
  theme x icon size x display mode. `tests\test_regs.py` gained checks for the
  vendor coverage, the vendored imports, pool gating, install confirmation,
  theme loading, tray variants, the item picker, the in-window settings, the
  no-scrollbar layout, the measured layout, the themed menu and carry-forward.

# SAITULS 0.1.2 (2026-09-01)

## What changed

- **LIMISAW tray is fully pixel, no blur at any DPI.** The icon is composed on a
  fixed 16x16 grid and then blown up by a **whole factor only** (nearest
  neighbour, `PixelOffsetMode.Half`, `SmoothingMode.None`) onto the canvas the
  shell asked for (`SystemInformation.SmallIconSize`), centred with a flat
  background. The shell no longer receives an odd-sized bitmap to resample.
- **Problip icons restored to the avatar image, strictly aliased.** The bell
  mask was replaced by the SAIPEN_OrangeShine avatar, downscaled with
  **NEAREST** resampling (no antialiasing) at every frame size (16/20/24/32/48/
  64/128/256) into `problip.ico` via `Scripts\make_pixel_ico.py`. Tray and
  window icons load the exact-size frame from that file (`AppIcon.For`), so the
  shell never resamples a smooth source. The runtime pixel-mask fallback and
  `Settings.IcoPath` removal were reverted; the file-based approach now
  guarantees aliased output at any DPI.
- **5-hour percentage now tells the truth when the weekly window is spent.**
  A 100% 5-hour bucket is unusable while weekly is at 0% — the tool is in a safe
  whose key only arrives when weekly refills. `Effective5h` reports **0%** for
  the 5-hour value whenever weekly is available and 0, in the tray number, the
  tray tooltip, the tray menu summary and the account panel (which additionally
  prints `locked by weekly` instead of the 5-hour reset time).
- Added `tests\pixel_purity.cs`: replays the LIMISAW render path at
  16/20/24/32/48/64 and asserts every opaque pixel is a literal Golden Default
  palette colour, plus five weekly-lock rule cases. `tests\test_regs.py` gained
  four matching source-contract checks (LIMISAW pixel scaling, Problip
  exact-frame icon loading, `make_pixel_ico.py` NEAREST resampling, weekly lock).

# SAITULS 0.1.1 (2026-09-01)

## What changed

- README and Wiki are now fully **English**; Cyrillic menu/UI labels are
  documented in English (actual `.reg` files keep their UTF-16 Cyrillic labels
  by design). Added GitHub community files: issue/PR templates, SECURITY.md,
  CONTRIBUTING.md; repo description and topics set; README gained badges,
  a feature matrix and a repository map.
- LIMISAW tray icon is rendered **non-antialiased at native 16×16** (was 32×32
  downscaled by the shell → blur). App icon is the `heh` avatar
  (`heh.ico`), embedded via `/win32icon:`. Program icon restored.
- LIMISAW `lowest` tray metric now shows the **minimum non-zero** remaining
  percentage (skips exhausted 5h windows, falls back to 0 only when all are 0).
- LIMISAW **quiet autostart**: `AutoStart=1`, registry Run key, launches
  `--minimized` into the tray.
- Problip gained a **0–100 volume slider** (replaced preset buttons); drag,
  click-to-jump, value saved to `problip.ini`.
- Codex account launchers hardened: `Start-Codex-Main.ps1` now explicitly sets
  `CODEX_HOME=$HOME\.codex` so inherited environment cannot open account 2/3
  under the main account. All three isolated launchers verified against their
  own profiles.
- AI_AGENT_LAUNCHER.PS1 `Get-StablePort` switched from FNV-1a (uint64 overflow)
  to a SHA-256-based deterministic port.
- `tests/test_regs.py` README contract updated to the English tokens.

# SAITULS 0.1.0 (2026-09-01)

- Added `INSTALL.cmd` and turned `setup.ps1` into a non-interactive,
  repeatable install/repair flow. Missing Python, FFmpeg, yt-dlp, aria2 and
  Deno are provisioned automatically; optional OpenCode/Cline menus no longer
  break the core installation when their CLIs are absent.
- Reorganized SAITULS around a Home readiness screen, clearer Explorer-menu
  names, full-row checkbox hit targets, safe dependency checks, destination
  selection for YouTube downloads, and non-elevated everyday launch.
- LIMISAW now has reliable title-bar dragging, one readable tray percentage
  (lowest remaining by default), a single refresh action, shortcut keys,
  double-click restore, single-instance behavior and quiet autostart.
- Destructive cleanup scripts now explain their scope and ask before deleting.
- Removed the personal `Ctrl+Delete` Task Manager hotkey; the native Windows
  `Ctrl+Shift+Esc` shortcut remains untouched.
- Rewrote README against the shipped behavior and current dependency model.

# SAITULS 0.0.2 (2026-09-01)

## What changed

- `SAITULS.exe` is now a pure launcher/installer hub. The embedded blip
  Monitor tab and its engine were removed; Problip stays in the repo as a
  standalone tool (`problip/Problip.exe`), launched from the Tools tab.
  Tabs: Menus | Tools | Settings.
- `LIMISAW.exe` is now tray-first: it draws the live limit numbers directly
  in the tray icon. The tray menu lets you pick which values to show per
  account — C1 5h / C1 weekly / C2 5h / C2 weekly, any combination. Added a
  reset detector (compares previous vs current remaining % and reset time to
  confirm an OpenAI-side reset) and quiet balloon notifications on reset.
  Reset times are shown as friendly relative values ("in 3h 20m") instead of
  raw ISO strings, and the accent palette (green/yellow/red) is brighter for
  readability on the dark surface.
- Problip autostart verified: per-user Run key points at the relocated exe,
  `AutoStart=1` in `problip.ini`, starts silently to tray.

# SAITULS 0.0.1 (2026-08-31)

First public release. The toolkit was consolidated from a scattered personal
setup into one repository and one GUI.

## What ships

- `SAITULS.exe` — Golden Default (Wintage) WinForms GUI compiled from
  `SAITULS.cs`: Menus / Monitor / Tools / Settings tabs.
- `LIMISAW.exe` — Codex rate-limit monitor (`LIMISAW.cs`): probes account 1
  (`~\.codex`) and account 2 (`~\.codex-account2`) via the codex app-server
  JSON-RPC protocol and shows the 5-hour and weekly remaining percentages
  with reset times. Read-only; never parses `auth.json` or touches tokens.
  Reachable from SAITULS's Tools tab (`Codex Limits`).
- 14 Explorer context-menu features (`Registry/`, installed via
  `Installers/INSTALL_ALL.PS1` or the GUI's Menus tab).
- OpenCode / Cline YOLO launchers, Codex cascaded menu.
- Problip standalone tray monitor (`problip/Problip.cs` -> `Problip.exe`).
- 33-locale string bundle + per-locale reg generation
  (`i18n/tools/gen_locale_reg.py`).
- Integrity suite `tests/test_regs.py`.

## Source-only repo

Heavy binaries (ffmpeg ~153 MB x2, ffprobe, yt-dlp, AV1/RTX40 builds,
Ghostscript, ExifCleaner) are NOT committed. Download
`SAITULS-payload-0.0.1.zip` from the release assets and extract into
`Bin\` / `Bin\App\` at the toolkit root — or run `BUILD_PAYLOAD.cmd`
against a full local tree.

## Known notes

- Context-menu commands are `%%ROOT%%`-tokenized; use
  `Registry/IMPORT_SAFE.PS1` (or `INSTALL_ALL.PS1`) to import — a raw
  `reg import` writes literal `%%ROOT%%` paths and breaks the menu.
- `___AHK/` (personal autostart scripts) and `.saipen/` (project memory)
  are intentionally absent from the public repository.
