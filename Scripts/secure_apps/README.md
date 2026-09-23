# Secure Apps

A FIDO2-gated launcher for applications whose data should not be readable
when the application is closed. Obsidian is the reference profile; nothing in
the code is Obsidian-specific.

SAITULS itself owns none of this. `SAITULS.cs` has one `Secure Apps` button
and prints one status line read from a non-secret state file — the same
boundary `Scripts\saipatch` and `Scripts\scenarios` use.

---

## The two clocks

The single idea the Default policy exists to express:

| | what it bounds |
|---|---|
| **authentication session lifetime** | how long the broker may reuse the decrypted volume secret without asking for the key again |
| **vault mounted lifetime** | how long the plaintext filesystem is actually reachable |

They are not the same thing. In Default mode the session survives six idle
hours, but **the vault is detached the moment the protected application
exits**. Reopening inside the session remounts without a key interaction; the
data was not sitting readable in between.

"Idle" means six hours without **protected** activity — a protected launch,
the protected application owning the foreground window, or an explicit Secure
Apps action. Using an unrelated program keeps nothing alive.

---

## Architecture

```
SAITULS.cs  ──button──>  SECURE_APPS.ps1  ──>  secure_apps.pyw  (thin launcher)
                                                    │
                                                    v
                                              secure_apps_gui.py (window + broker host)
                                                    │
                                                    ├── sa_broker.py     state machine, sessions, policy
                                                    ├── sa_auth.py       ISecureAuthProvider + providers
                                                    ├── sa_storage.py    ISecureStorageBackend + backends
                                                    ├── sa_crypto.py     HKDF / AES-GCM / SecretBuffer
                                                    ├── sa_applife.py    protected process tree
                                                    ├── sa_state.py      durable non-secret state + reconcile
                                                    ├── sa_audit.py      allowlist audit log
                                                    ├── sa_enroll.py     enrollment + recovery material
                                                    ├── sa_migrate.py    plaintext migration state machine
                                                    ├── sa_config.py     versioned fail-closed registry
                                                    ├── sa_paths.py      canonicalization + containment
                                                    ├── sa_privtask.py   the fixed, pre-approved elevation task
                                                    └── sa_privhelper.py elevated storage channel
                                                            │
                                                            │   Task Scheduler starts, kernel verifies
                                                            │
                                    %ProgramData%\SAITULS\secure-apps\
                                        privileged-helper.json      the pin (Administrators-owned)
                                        privileged-tmp\             protected scratch (SYSTEM+Admins only)
                                        privileged\                 the PROTECTED RUNTIME
                                            sa_storage_helper.ps1   (elevated)
                                            privileged-runtime.json bundle manifest
                                            fido-worker\            PyInstaller --onedir bundle
                                                sa_fido_worker.exe  (elevated, CTAPHID)
                                                _internal\...       every DLL it loads
```

`sa_cli.py` is the same machinery without a window: `selftest`, `status`,
`profiles`, `capabilities`, `enroll`, `import-app`, `open`, `lock`,
`mode`, `recover`, `migrate`, `migrate-status`, `migrate-recover`,
`audit`, `privileged-helper`.

### Why the broker is not elevated

Attaching a VHDX and unlocking a BitLocker volume need administrator rights.
Launching Obsidian must not have them. So the broker stays at medium
integrity and delegates exactly those two operations to
`sa_storage_helper.ps1`, which runs elevated and serves the broker over a
named pipe.

That pipe exists for one reason: the volume unlock secret has to cross a
process boundary, and the alternatives are a command line, an environment
variable or a temp file. All three are forbidden.

The elevated helper is started by a **pre-approved Scheduled Task**, not by a
consent dialog — see [Silent privileged start](#silent-privileged-start).
Opening, closing, locking and relocking a vault raise no UAC prompt at all.

The boundary, exactly:

| component | integrity |
|---|---|
| `secure_apps_gui.py` GUI (started by `secure_apps.pyw`) | **medium** |
| broker (`sa_broker.py`) and everything it imports | **medium** |
| `sa_cli.py`, migration included | **medium** |
| the protected application (Obsidian) | **medium** |
| `sa_storage_helper.ps1` (attach, unlock, mount, unmount) | elevated |
| `fido-worker\sa_fido_worker.exe` (CTAPHID, launched by the helper) | elevated |

Two processes are elevated, both of them narrow, both of them started through
one fixed scheduled task and verified from the kernel by the broker.
Everything a person interacts with is not.

This is enforced, not merely intended: the hardware acceptance run refuses to
start from an elevated process, and `sa_cli.py migrate` refuses to migrate
from one. See [Hardware acceptance](#hardware-acceptance).

### Silent privileged start

One UAC prompt per broker session sounds modest. In practice it is a consent
dialog on every unlock, every lock, every mount and every relock — including
the relock that fires when Windows locks the workstation, which nobody is
there to approve. A person trained to click *Yes* on a dialog they did not
ask for is a worse outcome than the prompt was ever worth.

So the elevation is approved **once**, for **one fixed action**:

```
\SAITULS\SecureAppsPrivilegedHelper        RunLevel  HighestAvailable
                                            LogonType InteractiveToken
                                            Triggers  (none: on demand only)
  powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass
                 -WindowStyle Hidden
                 -File "<pinned>\sa_storage_helper.ps1" -ScheduledHelper
```

After that registration the medium-integrity broker may ask Task Scheduler to
start *that task* — and nothing else — with no second dialog. Global UAC is
untouched: `EnableLUA` stays `1`, `ConsentPromptBehaviorAdmin` and
`PromptOnSecureDesktop` stay whatever the user chose, and every other program
on the machine keeps prompting exactly as before. This is a narrow
pre-authorized privilege broker, not a system-wide UAC bypass.

Four properties make it a boundary rather than a hole:

**The action is fixed.** The task accepts no caller-supplied executable, no
script path and no arguments. Ordinary runtime may only `schtasks /Run` the
registered task and read its state; `/Create`, `/Change` and `/Delete` are
setup operations that need elevation again. `sa_privhelper.py` contains no
`runas`, no `ShellExecute` and no task mutation — enforced by a test.

**The definition is fingerprinted.** `sa_privtask.canonical_definition`
reduces a registered task to what decides *what runs, as whom, at what
privilege*: action image, argument tokens, working directory, principal SID,
run level, logon type, triggers, action count, and the settings that could
starve or kill the helper (`AllowStartOnDemand`, `MultipleInstancesPolicy`,
`ExecutionTimeLimit`, the battery pair). `definition_fingerprint` hashes that
canonical form, the installer pins it, and every silent start re-checks it. A
task whose action, principal, run level or arguments moved is
`PRIVILEGED_TASK_TAMPERED`: **fail closed, and never repaired from a
medium-integrity process.** Repair is an explicit privileged operation.

**What the task cannot carry, a protected runtime and a pinned file carry.**
A fixed action has no command line, and an elevated process must not run
anything a medium-integrity account can reach. So an elevated Install/Repair
transaction copies a **protected runtime** into
`%ProgramData%\SAITULS\secure-apps\privileged`:

```
privileged\
    sa_storage_helper.ps1        the elevated helper (the task's -File)
    privileged-runtime.json      the public bundle manifest
    fido-worker\                 the FIDO worker, PyInstaller --onedir
        sa_fido_worker.exe
        _internal\...            every DLL and module it loads
```

The whole tree carries one explicit ACL — SYSTEM and Administrators full
control, the intended user read+execute, inheritance off, **owner
Administrators** — so a medium account may execute the runtime and can neither
modify, replace, rename nor delete anything in it.

The worker is `--onedir`, never `--onefile`, and that is a security decision
rather than a packaging preference: a onefile executable is a self-extracting
stub that unpacks its entire dependency closure into an ordinary
`%TEMP%\_MEIxxxxxx` directory and loads it from there. Running that elevated
would mean an elevated process loading its code out of a directory the
medium-integrity user owns — exactly what the protected runtime exists to
prevent. Nothing is extracted at runtime.

Binding one executable's SHA-256 is not enough for a onedir tree, because its
DLLs and Python extension modules are separate files that the elevated process
also loads. `fido_worker_bundle_fingerprint` therefore binds **every file in
the bundle**: relative path, size and SHA-256, canonically serialised (paths
relative to the bundle root, `/` separators, lowercased, printable ASCII,
sorted ordinally, JSON with sorted keys and no insignificant whitespace, no
timestamps and no build-machine paths) and hashed. Any modification, addition
or deletion inside the bundle moves that value and invalidates installation
and acceptance. The same value is computed identically by
`sa_privtask.bundle_fingerprint`, by the elevated helper's
`Get-BundleFingerprint`, and by `build_frozen_worker.ps1`.

The **pin**, `%ProgramData%\SAITULS\secure-apps\privileged-helper.json`, is
what a fixed action cannot carry: the protected runtime root, the installed
helper and its SHA-256, the worker bundle directory and its recursive
fingerprint, the worker executable and its SHA-256, the runtime bundle
fingerprint, the pipe name derived from the account SID, and the idle timeout.
It carries **no interpreter and no worker source path** — there is no
interpreter on the elevated runtime path any more. Its ACL is explicit
(Administrators and SYSTEM full control, the intended user's own SID read) and
so is its **owner**: Administrators. Ownership matters as much as the DACL,
because an object's owner holds `WRITE_DAC` implicitly and could hand itself
write access at any time. The elevated helper refuses to start if its own
path or digest, the worker bundle's recursive fingerprint, or the runtime
manifest does not match the pin.

Privileged **command material** gets the same treatment. Creating a VHDX runs
DiskPart against a script file, and that file decides what an elevated
DiskPart does; it therefore lives in
`%ProgramData%\SAITULS\secure-apps\privileged-tmp\<transaction>\` (SYSTEM
and Administrators only, no medium-integrity access at all, owner
Administrators), and its path, reparse-point freedom, DACL and owner are
re-checked in the instant before DiskPart opens it — never in the medium
user's `%TEMP%`.

**The elevated helper chooses what it loads.** PowerShell auto-loads a module
for an unqualified command from `$env:PSModulePath`, whose first entry is
normally the CurrentUser directory under `%USERPROFILE%\Documents` — which a
medium-integrity account owns. Before any Storage or BitLocker command can
auto-load, the helper rebuilds that search path from machine locations only
(resolved through the known-folder API, never the environment block, because a
user-scope `%SystemRoot%` or `%ProgramFiles%` wins in the merged block a
scheduled task inherits), imports `Storage` and `BitLocker` from absolute
system paths, confirms each module really came from
`%SystemRoot%\System32\WindowsPowerShell\v1.0\Modules\<name>`, and **fails
closed** if it cannot. Every storage command is then written module-qualified
(`Storage\Get-Disk`, `BitLocker\Unlock-BitLocker`), so resolution never
depends on a same-named user module losing a race. External executables are
launched by absolute, verified path (`%SystemRoot%\System32\diskpart.exe`),
never by name through `PATH`. `-SelfTest` reports `trusted_module_path`,
`storage_module_trusted`, `bitlocker_module_trusted` and `diskpart_path`.

**Install and Repair are transactional.** Every mutation point is a named
stage — stage copy, runtime ACL application, runtime ACL measurement, runtime
swap, task registration, task security descriptor, pin save, pin hardening,
final verification, commissioning — and the previous installation (runtime,
pin, task XML, task security descriptor, task state) is snapshotted before the
first of them. A failure at any stage rolls the machine back to that snapshot
and re-verifies the result: a first install rolls back to no task, no pin and
no protected runtime; a repair rolls back to the exact previous installation.
A final verification that does not pass is a **failed transaction**, not a
warning — no runnable task is ever left pointing at a runtime whose ACL was
not proven, and `harden_runtime_dir() == True` plus
`runtime_acl_report()["runtime_acl_valid"] is True` are required *before* the
task is registered, with an unmeasurable ACL counting as failure rather than
as success. If the rollback itself cannot be completed the error carries
`REPAIR_REQUIRED`.

**The task folder is part of the boundary.** Deleting a registered task needs
write access to the FOLDER that contains it, not to the task object, so
`\SAITULS` gets the same explicit descriptor as the task itself. Both are
written with `TASK_DONT_ADD_PRINCIPAL_ACE`: without that flag Task Scheduler
appends its own ACE for the run-as account and, in doing so, silently drops
the `SE_DACL_PROTECTED` flag the supplied SDDL carried.

**Starting is not authenticating.** Task Scheduler is how the high-integrity
process comes into existence; it is not why the broker trusts it. Before a
single byte of secret is written, the broker asks the kernel who is serving
the pipe — `GetNamedPipeServerProcessId`, image path, `TokenElevation` plus
integrity level, token user, session — and cross-checks the helper's greeting
against those measurements. A medium-integrity impostor that squats the pipe
name fails on the one claim it cannot fake: being high integrity.

Lifetime is a **lease**, not a process. The helper is held while a volume is
attached or a privileged transaction is in flight, and dropped otherwise; the
helper then exits by itself after `idle_timeout_seconds` (default 300) with
nobody connected. The next unlock starts it again, silently, through the same
task. A stray elevated process does not sit there between unlocks, and
getting one back costs no dialog.

Locking never depends on consent. `Lock Now`, workstation lock, logoff,
suspend, shutdown and the idle relock all start the helper the same silent
way; when it genuinely cannot be started, the broker surfaces
`LOCK_PRIVILEGED_HELPER_UNAVAILABLE` and an `ERROR` state. It does **not**
report `LOCKED` — a comfortable word over a volume that may still be mounted
is the one failure mode worse than an honest error.

### Setup operations

**Build input, first.** The privileged runtime ships a frozen FIDO worker, so
`install` and `repair` need the `--onedir` bundle to exist beside the source
(or under `dist\`). It is machine-specific and is not committed:

```
python -m pip install pyinstaller
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\secure_appsuild_frozen_worker.ps1
```

That writes `Scripts\secure_appsido-worker\` (`sa_fido_worker.exe` plus
`_internal\`) and prints the `fido_worker_bundle_fingerprint` the installer
will pin. Without it, `install` refuses: an interpreter on the elevated
runtime path is exactly what the protected runtime replaced.

Then four explicit operations, each of which may raise one ordinary consent
dialog. They are the only elevation Secure Apps ever requests.

```
python Scripts\secure_apps\sa_cli.py privileged-helper preflight
python Scripts\secure_apps\sa_cli.py privileged-helper check [--start]
python Scripts\secure_apps\sa_cli.py privileged-helper install [--idle-timeout 300]
python Scripts\secure_apps\sa_cli.py privileged-helper repair
python Scripts\secure_apps\sa_cli.py privileged-helper remove
python Scripts\secure_apps\sa_cli.py privileged-helper prepare-uac --confirm
```

The same four sit behind **Privileged helper** in the Secure Apps window.

`install` is the one-time step, and it is deliberately two-sided: the
elevated child registers the task and writes the pin, and then *this*
medium-integrity process decides whether what came back is really a silent
privilege boundary — definition valid, helper started by Task Scheduler,
helper high integrity, **broker still medium integrity** — and stops the
helper again if it is not (`sa_privtask.commission`). An installation
validated by an elevated process would prove nothing: there was no boundary
to cross.

`remove` removes the silent elevation mechanism and nothing else. The
encrypted vault, the FIDO2 credential, Obsidian's data, the acceptance record
and the recovery material are untouched.

`prepare-uac` exists for a host where `EnableLUA` is already `0`. There is no
medium-integrity broker on such a machine — every administrator process is
high integrity — so Secure Apps refuses to install the task there, and offers
this one explicit action instead: set `EnableLUA=1`, warn that Windows must be
restarted, change **no other UAC setting**, and resume setup after the reboot.
It is never performed silently and never as a side effect of anything else.

### UAC diagnostics

```
python Scripts\secure_apps\sa_cli.py privileged-helper preflight
```

reports `EnableLUA`, `ConsentPromptBehaviorAdmin`, `PromptOnSecureDesktop`
(read, never required), this process's integrity level, whether the account
may register the task, whether the task is installed, valid and runnable, the
pinned and installed definition fingerprints, whether the pin is writable by a
non-administrator, the helper idle timeout, and — once a helper is connected —
its integrity and the pipe authentication state.

The healthy target state:

```
EnableLUA          = 1
SecureBroker       = MEDIUM
Obsidian           = MEDIUM
PrivilegedHelper   = HIGH
FidoWorker         = HIGH
SilentTask         = VALID
```

No particular UAC slider position is required, and none is set.

---

## Key hierarchy

```
FIDO2 credential
    -> hmac-secret output              never persisted
    -> HKDF-SHA256(salt, profile ctx)  domain separation per profile+container
    -> Key Encryption Key              never persisted
    -> AES-256-GCM unwrap              AAD = profile id | container id
                                             | credential id | schema version
    -> BitLocker volume unlock secret  never persisted
    -> BitLocker encrypted VHDX
```

Only ciphertext and public metadata reach disk (`state\credentials.json`).
The raw hmac-secret output, the derived KEK and the unwrapped volume secret
live in `sa_crypto.SecretBuffer` and are zeroized on every exit path.

**hmac-secret is mandatory.** An authenticator without the CTAP2
`hmac-secret` extension cannot produce key material, so enrollment fails with
a capability message rather than silently downgrading an encrypted vault to a
presence check. A future gate-only WebAuthn provider must set
`provides_key_material = False`, and the broker refuses to use such a
provider for a profile with encrypted storage.

### Authentication transports

`yubikey-fido2-hmac-secret` is written against **python-fido2 2.x**
(`fido2>=2.0,<3`). An install outside that range is refused by name, at load
time, rather than discovered when somebody plugs in a key. Exactly one
transport is chosen per host and it is **reported**, never assumed:

| transport | what it is | when it is usable |
|---|---|---|
| `WINDOWS_WEBAUTHN` | `fido2.client.windows.WindowsClient` with `allow_hmac_secret=True`, in the medium-integrity broker | the Windows WebAuthn API can carry an hmac-secret salt: `WebAuthNGetApiVersionNumber() >= 6` |
| `ELEVATED_CTAP_HELPER` | the frozen `fido-worker\sa_fido_worker.exe` speaking CTAPHID (`DefaultClientDataCollector`, a `UserInteraction`, `HmacSecretExtension(allow_hmac_secret=True)`) inside the verified elevated helper, driven from the broker over the privileged pipe | Windows, where the platform API cannot carry the salt but the helper can reach the authenticator through direct CTAP |
| `DIRECT_CTAP` | the same `Fido2Client`, in this process | not Windows |
| `UNAVAILABLE` | none of the above is usable | `detail` names the precondition that failed |

Two facts about Windows decide this, and they pull in opposite directions:

* the platform API can only carry an hmac-secret salt from
  `WebAuthNGetAssertionOptions` version 6, and **Windows 10 22H2 ships
  `webauthn.dll` API version 2** — which can create an hmac-secret
  credential and can never read key material back out of it;
* Windows 10 1903+ hides FIDO HID devices from medium-integrity processes,
  so the broker cannot simply talk CTAPHID itself.

`ELEVATED_CTAP_HELPER` is the bridge between them, and it is what a Windows
10 host actually uses. The CTAP conversation runs in the elevated FIDO
worker, which can see the device; the broker, the GUI and the protected
application stay at medium integrity, and the PIN travels over the
privileged pipe rather than a command line or an environment variable.
**Windows 10 is a supported host.** `UNAVAILABLE` is reported only when
neither path works — not for an old `webauthn.dll` on its own.

A `WindowsClient` failure is *never* retried over HID: one transport is
chosen per host, and it is reported rather than assumed.

Check what a host can actually do:

```
python Scripts\secure_apps\sa_cli.py capabilities obsidian
```

### User verification

`authentication.user_verification` is `required`, `preferred` or
`discouraged`, and it defaults to **`required`** — a profile exists to keep
data unreadable, and at `discouraged` a stolen key is the whole secret. At
`required`:

* the credential is created with UV required, and enrollment fails with a
  capability message if the authenticator has no FIDO2 PIN and no built-in
  verification;
* **every** assertion is checked for the UV bit in the returned authenticator
  data, so an authenticator that quietly answers with presence only does not
  open the vault.

On a YubiKey without biometrics this means FIDO2 PIN plus touch. The PIN is
never stored, never logged, and never crosses a command line or an
environment variable: on Windows WebAuthn the platform collects it in its own
native dialog and it does not enter the broker process at all; on the direct
CTAP path a caller-supplied callback returns it straight to python-fido2.

### Losing a key

The volume secret is *wrapped*, not *derived*. That is what makes a second
key cheap: `Manage key -> Add another key` unwraps with key 1 and writes a
second wrapped copy for key 2. The vault is **not** re-encrypted. Both
enrollments share one hmac-secret salt, so unlocking a two-key vault is still
one assertion and one touch.

The BitLocker recovery material is shown **once**, at first enrollment, and
is never written anywhere by SAITULS. If it is not explicitly acknowledged,
the brand-new empty container is rolled back rather than left behind
protected by a single key.

---

## Policies

### `default`

* first protected launch requires the key;
* the decrypted secret stays in broker memory afterwards;
* **the vault is detached as soon as the application exits**;
* reopening inside the session remounts without another touch;
* six hours without protected activity ends the session, closes the
  application through the secure-close sequence and relocks.

### `aggressive`

Every launch requires the key, unless the exact protected application is
already running as part of the current launch transaction. On exit: unmount,
zero the secret, invalidate the session.

### System events

`workstation_lock`, `logoff`, `suspend`, `shutdown`, `broker_shutdown`,
`manual_lock`. Each is gated by its own `lock_on_*` flag; `Lock Now` is
unconditional. For the Obsidian profile all of them secure-lock.

### The secure-lock sequence

1. stop accepting new protected launches
2. request graceful close
3. bounded wait
4. forced termination, if the profile allows it
5. **confirm the whole process tree exited**
6. flush filesystem state
7. unmount / detach
8. zero the in-memory secret
9. mark LOCKED

Step 5 is a gate, not a formality: storage is never detached while a process
in the tree is alive. Obsidian is Electron — the parent can exit while a
renderer still holds a handle on the vault.

### Typed conditions

No scripting language. A condition is a name from a fixed table plus its
declared parameters:

| condition | effect |
|---|---|
| `require_auth_after_windows_lock` | key required again after a workstation lock |
| `require_auth_after_suspend` | …after resume |
| `require_auth_after_logoff` | …after logoff |
| `require_auth_after_app_exit` | …after the application exits (Default only; Aggressive does this anyway) |
| `require_auth_after_session_expiry` | …after the idle session expires |
| `require_auth_after_minutes` `{minutes}` | hard session lifetime cap, regardless of activity |
| `close_app_on_idle` | close the protected application at idle even when storage is not mounted |
| `unmount_storage_on_app_exit` | condition form of `policy.unmount_when_app_closes` |
| `require_auth_provider` `{provider}` | refuse to run under any other provider |

Unknown condition types, unknown keys anywhere in a profile, unknown schema
versions, unknown backends and unknown providers all **fail closed**.

---

## Storage backend contract

`sa_storage.SecureStorageBackend`: `state`, `create`, `unlock_and_mount`,
`unmount`, `start`, `stop`. Four states:

| state | meaning |
|---|---|
| `missing` | no container exists yet |
| `detached` | container present, nothing attached — this is LOCKED |
| `attached_locked` | attached but BitLocker has not released it — a recovery condition |
| `mounted` | plaintext filesystem reachable at the mount path |

The shipped backend is `bitlocker-vhdx`: a data VHDX holding NTFS, protected
by BitLocker XTS-AES-256, mounted onto a **directory mount point** so the
vault keeps its historical path. BitLocker operations address the volume by
its `\\?\Volume{GUID}\` path, so the vault never has to be exposed at a drive
letter just to be unlocked.

`New-VHD` lives in the Hyper-V module, which a Pro workstation need not have,
so the container file is created with `diskpart` — whose script carries a
path and a size and no secret.

---

## Privileged helper protocol

Newline-delimited UTF-8 JSON over `\\.\pipe\SAITULS_SECAPP_PRIV_<sid hash>`,
one response per request, one client at a time.

The **helper owns the pipe** and the broker connects to it. That is a
consequence of the fixed task action: it cannot carry a per-spawn pipe name,
so the name is derived from the account SID by both sides independently (it is
not a secret and authenticates nothing — it only keeps two users' helpers
apart). The broker opens the connection at `SECURITY_IDENTIFICATION`, so the
elevated helper may identify it and can never impersonate it elsewhere.

```
->  {"id":N,"op":"<op>","args":{...}}
<-  {"id":N,"ok":true,"result":{...}}
<-  {"id":N,"ok":false,"error":"<category>","message":"<text>"}
```

| op | args | result |
|---|---|---|
| `ping` | – | `{"pong":true}` |
| `state` | `container`, `mount_path` | `{"state":..., "mounted_at":...}` |
| `create` | `container`, `mount_path`, `size_gb`, `label`, `secret_b64` | `{"recovery_password":...}` |
| `unlock_mount` | `container`, `mount_path`, `secret_b64` | `{"state":"mounted"}` |
| `unmount` | `container`, `mount_path` | `{"state":"detached"}` |
| `helper_info` | – | the greeting: pid, elevation, SID, task, fingerprint |
| `shutdown` | – | – |

Error categories: `storage_busy`, `storage_create_failed`,
`storage_unlock_failed`, `storage_mount_failed`, `storage_unmount_failed`,
`internal`. Exception text is never forwarded — it is the classic place a
secret leaks from.

**Peer authentication, both directions.** `{"elevated": true}` in a greeting
is a claim, not evidence, and being started by Task Scheduler proves nothing
about the process that actually answered. So both ends measure, and both fail
closed.

The broker, before a single byte of secret is written:

* the helper's pipe carries an explicit DACL: this user, Administrators,
  SYSTEM. No inherited default, no Everyone ACE;
* `GetNamedPipeServerProcessId` gives the serving pid;
* its image path must be the Windows PowerShell host recorded in the
  installation pin (`%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`,
  or the WOW64 twin; `SAITULS_SECURE_APPS_HELPER_IMAGE` widens the allow-list
  for a packaged host);
* its token must report **both** `TokenElevation` and an integrity level of
  high or above — the one claim a medium-integrity impostor cannot fake;
* it must run as **this account**, in **this session**;
* the greeting's `pid`, `elevated`, `user_sid` and task-definition fingerprint
  are then cross-checked against those measurements and against the pin, and a
  disagreement closes the pipe.

The helper, before it serves anything (`Test-ClientAcceptable`): it
impersonates the caller for identification only, and requires the same account
at **medium** integrity. An elevated client is refused — a high-integrity
broker means the architecture this vault depends on is not in force. The
integrity level is read from the token through `GetTokenInformation`, because
.NET's `WindowsIdentity.Groups` does not expose the mandatory label at all; a
host where that probe cannot be built serves nobody and says so in `-SelfTest`.

`call()` refuses to transmit anything at all until the broker-side
verification has completed, so a future refactor that skips it fails closed.
The helper also creates its pipe only when nothing else owns that name, and
validates its own script, the interpreter and the FIDO worker against the
SHA-256 values in the pin before it opens anything.

Preflight without elevation or hardware:

```
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\secure_apps\sa_storage_helper.ps1 -SelfTest
python Scripts\secure_apps\sa_cli.py privileged-helper preflight
```

The helper's self-test also reports `launch_mode`, the pin path, whether the
pin is present and valid, the derived pipe name, the worker bundle directory
and its recursive fingerprint, `trusted_module_path`,
`storage_module_trusted`, `bitlocker_module_trusted`, the resolved
`diskpart_path`, the protected scratch root, and whether the integrity probe
can be built on this host. `-PinPath` is accepted **only** together with
`-SelfTest`: in production the path is a constant, because an elevated process
that lets a caller choose its pin file lets the caller choose what it runs.

---

## FIDO2 helper contract (optional)

Not to be confused with the `ELEVATED_CTAP_HELPER` transport above, which is
how the shipped `yubikey-fido2-hmac-secret` provider reaches an authenticator
on Windows. This section is a *different* provider, and nothing uses it by
default.

`external-helper-fido2-hmac-secret` delegates the provider interface to a
separate executable — for hosts that would rather ship a modern .NET/WebAuthn
binary than a Python HID stack. One JSON object on stdout per invocation,
exit 0 on success. **Key material travels on stdout, never on a command
line.**

```
<helper> capabilities
    -> {"ok":true,"hmac_secret":true,"authenticators":1,"detail":"..."}

<helper> create --rp-id <id> --user-name <name> --profile <p> --require-hmac-secret
    -> {"ok":true,"credential_id":"<base64url>","user_id":"<base64url>"}

<helper> hmac --rp-id <id> --salt <base64url> --credential-id <base64url> [...]
    -> {"ok":true,"credential_id":"<base64url>","output":"<base64url>"}

failure (all three)
    -> {"ok":false,"error":"capability|cancelled|wrong_credential|unavailable|failed",
        "message":"..."}
```

Point a profile at it with
`"provider": "external-helper-fido2-hmac-secret"` plus `"helper_executable"`.

---

## Managed storage layout

```
<managed-root>\
  apps\obsidian\app\          imported portable application (optional)
  vaults\obsidian\            obsidian.vhdx
  state\
    secure-apps.json          durable NON-SECRET lifecycle state
    credentials.json          wrapped keys: ciphertext + public metadata only
    audit.log                 allowlist JSONL
    migration-<profile>.json  migration journal
    sessions\
```

Default root: `%LOCALAPPDATA%\SAITULS\secure-apps`. Override with
`SAITULS_SECURE_APPS_ROOT` (the environment outranks the registry, so a
relocated install never has to edit tracked source).

**None of this is committed.** `.gitignore` and the AUDAPACK/payload build
exclude the managed root, imported binaries, containers, credentials and
generated secrets.

---

## Crash recovery

The state file records facts, never secrets: profile id, container path,
mount path, expected state, last transition, last process id, broker pid.

Reconciliation is deliberately pessimistic, because the opposite mistake is
the dangerous one — **a crashed broker does not imply a locked vault**:

* volume mounted with no live broker owning it → `RECOVERY_REQUIRED`
* recorded exposure with no live broker → `RECOVERY_REQUIRED`
* container `attached_locked` → `RECOVERY_REQUIRED`
* unreadable or corrupt state file → `RECOVERY_REQUIRED`

`RECOVERY_REQUIRED` refuses new launches until `Recover` completes a safe
relock.

---

## Hardware acceptance

Everything above is hermetically tested. Migrating a real vault is the one
operation whose failure modes are not: it needs the physical authenticator,
the elevated helper, BitLocker and the lock policy to behave on *this* host.
So `sa_cli.py migrate` opens only against a durable record written by a
disposable run that proved them, against a throwaway 1 GiB container and
never a configured vault:

```
python tests\secure_apps_interactive.py --disposable-acceptance
```

Checks `P01`..`X02` cover the silent privilege boundary (the task is
installed with the expected fixed definition, Task Scheduler starts the helper
with **no** consent prompt, the helper arrives at high integrity while the
broker stays medium, and a changed definition is refused); the transport, the
authenticator, hmac-secret,
user verification, credential creation, unwrap, and the four ways
authentication must fail closed (cancelled PIN, wrong PIN, no key, wrong
credential); the BitLocker container and its detach behaviour; Default and
Aggressive reopen; a real workstation-lock event; the death of the elevated
helper and the recovery after it; and an audit-log secret scan. One skipped,
cancelled or failed check writes a record that authorises nothing.

The record (`state\hardware-acceptance.json`) is public facts and booleans,
built from a closed allowlist. It is refused as stale as soon as anything it
vouched for moves:

| bound | refused when |
|---|---|
| host identity | the machine GUID or host name changed |
| broker integrity | the run, or the migration, is **not** medium integrity |
| transport, hmac-secret semantics | a `webauthn.dll` that learns the salt moves the host to `WINDOWS_WEBAUTHN` |
| python-fido2 major version | 2.x → 3.x |
| implementation fingerprint | any source in `sa_acceptance.SECURITY_SOURCES` changed |
| acceptance producer fingerprint | `tests\secure_apps_interactive.py` changed or is unreadable |
| profile security fingerprint | any security-relevant field of the profile changed |
| privileged launch mode | the elevation mechanism itself changed |
| privileged task name | the helper is started through a different task |
| privileged task definition | the registered task's canonical definition moved, or it is gone |

The last two are there for the obvious reasons. Fingerprinting only the code
under test would let a weakened *runner* — the program that decides whether a
check passed — keep vouching for the old verdict. Binding only the provider,
the UV policy and the backend would let the lock policy itself be rewritten
after acceptance: the profile fingerprint is a SHA-256 over canonically
serialised security-relevant profile data (sorted keys, stable condition
order, UTF-8, no insignificant whitespace), covering the mount path, the
container, the launched application, the policy mode, the idle timeout, every
`lock_on_*` and unmount/terminate flag, the timeouts and the typed conditions
with their parameters. Cosmetic data — the label, the notes, the container
size, the filesystem label — is deliberately outside it, so a rename does not
cost a run with the key in hand.

Medium integrity is required, not merely matched. An acceptance run started
from an elevated process prints

```
FAIL: BROKER_MUST_RUN_MEDIUM_INTEGRITY
```

and creates neither an enrollment nor a container; a record that recorded an
elevated broker never grants readiness; and an elevated `migrate` is refused
even if the record agrees with it. The elevated storage helper and FIDO
worker are unaffected — they are the part that is *supposed* to be elevated.

**A host with UAC disabled cannot pass this.** With `EnableLUA=0` there is no
filtered token, so every process of an administrator is high integrity and
there is no ordinary shell to start from — the boundary the vault is
protected with does not exist on that machine. Re-enable UAC (`EnableLUA=1`,
then reboot) or use a standard user account. The probe reports it:

```
python tests\secure_apps_interactive.py --probe
```

Test the gate without a key, a mount or a copy:

```
python Scripts\secure_apps\sa_cli.py migrate obsidian --check-acceptance
```

```
HARDWARE_ACCEPTANCE_RECOGNIZED
FIDO2_HARDWARE_ACCEPTED
STORAGE_ACCEPTED
MIGRATION_READY
```

Anything else prints `MIGRATION_BLOCKED: HARDWARE_ACCEPTANCE_REQUIRED`, every
reason it was blocked, and the exact command that would fix it. The gate runs
**before** the single-instance mutex, before a broker exists and before the
elevated helper is started: a refused migration asks for no key and raises no
UAC prompt.

---

## Migration

```
PRECHECK -> CREATE -> COPY -> VERIFY_TREE -> VERIFY_SIZES -> VERIFY_HASHES
    -> VERIFY_APP -> CLOSE_APP -> RELOCK -> REMOUNT -> REVERIFY -> CUTOVER
```

The encrypted destination is mounted at a **staging** path while the
plaintext original keeps owning the real one; cutover renames the plaintext
aside and remounts the volume at the real path. Note content is never
inspected, parsed, normalised or rewritten — files are copied byte for byte,
through the `\\?\` long-path namespace, with no ZIP/7z repack anywhere.

Migration is destructive only to a *name*:

```
MIGRATION_VERIFIED
PLAINTEXT_SOURCE_REMAINS
```

The second is reported until the user removes or archives the old copy
themselves. **The gate does not claim the vault is protected while a readable
plaintext copy still exists.**

An interruption is repairable in both windows. Before the cutover the
plaintext still owns its path and the run resumes from the journal. Inside
the cutover, `sa_migrate.recover` can tell the two possible states apart and
either finishes the mount or renames the plaintext back.

```
python Scripts\secure_apps\sa_cli.py migrate obsidian --source "%USERPROFILE%\Documents\SAITULS Vaults\Obsidian"
python Scripts\secure_apps\sa_cli.py migrate-status  obsidian
python Scripts\secure_apps\sa_cli.py migrate-recover obsidian
```

---

## Logging

Allowlist, not denylist. A record is built from a fixed field set
(`sa_audit.ALLOWED_FIELDS`) and anything else is dropped silently — silently,
because a caller that accidentally passes `secret=...` must not turn the
mistake into an exception carrying the value.

Allowed: profile id, transition, timestamp, pid, mount target, auth provider
name, credential id **hash**, result, error category.
Forbidden and structurally unreachable: hmac-secret output, BitLocker
password or recovery secret, PIN, raw credential material, vault content,
tokens.

Exception objects never reach the log; callers pass a category from
`sa_audit.ERROR_CATEGORIES`.

---

## Tests

```
python tests\test_secure_apps.py                  lifecycle, enrollment state
                                                  machine, user verification,
                                                  migrate entry point,
                                                  GUI threading regressions
python tests\test_secure_apps_acceptance.py       the hardware-acceptance gate:
                                                  record allowlist, every
                                                  fingerprint, medium integrity
python tests\test_secure_apps_hygiene.py          secret-hygiene regressions
python tests\test_secure_apps_privtask.py        the privilege boundary: task
                                                  definition canonicalization,
                                                  fingerprint, tamper detection,
                                                  peer identity, silent start,
                                                  idle lease, lock/unlock
python tests\test_secure_apps_fido2_contract.py   python-fido2 API contract
python tests\test_consoles_launcher.py            console launcher tests
powershell -File Scripts\secure_apps\SECURE_APPS.ps1 -SelfTest
powershell -File Scripts\secure_apps\sa_storage_helper.ps1 -SelfTest
```

All of the above are hermetic: fake authentication provider, fake storage
backend, injected clock, injected process adapter. No security key, no VHDX,
no elevation, no waiting.

`test_secure_apps_fido2_contract.py` is the one that imports the **real**
`fido2` package, and CI installs it. It constructs the whole 2.x client
surface against a stub CTAP2 device that answers `authenticatorGetInfo` and
nothing else. An absent dependency is not a passing test: while CI omitted
`fido2`, the provider's only executed line was its `ImportError` handler,
which is exactly how it stayed written against an API generation that no
longer existed.

The Qt tests need PyQt6 and skip without it. They run a real `QThread` and
real queued signals, because the two defects they pin down — an
acknowledgement that could not reach the thread waiting for it, and a close
that raced process termination — exist only across that boundary.

Real Windows storage, disposable, elevated, no security key:

```
python tests\test_secure_apps_windows_storage.py
```

It builds a real VHDX, enables BitLocker on it, and runs a first run against
a genuinely **non-empty** final mount directory: enrollment without touching
it, migration out of it, the cutover rename as the first thing that moves it,
the final mount only once it is empty, and the plaintext backup still there
at the end. Everything it makes lives under `TEMP` and is removed again. It
skips unless the privileged helper task is installed and valid, because the
elevated helper is started by that task and by nothing else.

The Windows behaviour a hermetic suite cannot stand in for, on a disposable
profile, with UAC left exactly as the user has it:

```
UAC enabled (EnableLUA=1)              broker medium integrity
helper task installed                  RunLevel HighestAvailable
broker starts the helper               with NO consent prompt
helper reports high integrity          protected application stays medium
BitLocker mount / unmount work         direct CTAP works
helper exits after the idle timeout    the next operation restarts it silently
```

None of it weakens a global UAC setting, and a passing hermetic suite is not
evidence for any line of it.

Hardware coverage is separate and explicitly interactive:

```
python tests\secure_apps_interactive.py --probe
python tests\secure_apps_interactive.py --disposable-acceptance
python tests\secure_apps_interactive.py --enroll    --profile obsidian
python tests\secure_apps_interactive.py --unlock    --profile obsidian
python tests\secure_apps_interactive.py --lifecycle --profile obsidian
```

`tests\test_secure_apps_acceptance.py` covers this file's hardware-free
parts — the gate bookkeeping, the PIN digest, the audit scanner, the
disposable registry — and never starts a hardware mode. It is also what
keeps the acceptance-producer fingerprint honest: if the check definitions
or the pass/fail aggregation ever move out of
`secure_apps_interactive.py`, that suite fails until
`sa_acceptance.ACCEPTANCE_PRODUCER_SOURCES` names the new module too.

Each mode writes a non-secret **evidence record** (`--evidence <path>`) built
from a closed allowlist: python-fido2 version, Windows WebAuthn availability
and API version, transport, authenticator detected, hmac-secret available,
user verification available, and PASS/FAIL for enrollment, unlock, default
reopen, aggressive reopen, workstation-lock relock and vault detached. A PIN,
an hmac-secret output, a volume password, BitLocker recovery material and a
wrapped key have no code path into that file.

CI never runs the interactive modes and must never require a YubiKey.

---

## First run

```
pip install PyQt6 cryptography pywin32 "fido2>=2.0,<3"
```

The sequence, end to end:

```
privilege separation         EnableLUA=1 (reboot) if this host has it off
privileged helper install    one consent dialog, ever: registers the fixed
                               scheduled task and pins its paths
probe                        what this host can actually do
disposable hardware          P01..X02 against a throwaway container,
  acceptance                   with the key in hand; writes the record
production profile           create the real container, enroll the key,
  enrollment                   store the recovery material
production migration         the gate, with no key and no mount
  gate check
real migration               copy, verify, cut over
```

**Run all of it from an ordinary shell.** Windows asks for consent exactly
once, when the privileged helper task is installed; an elevated shell is
refused by the acceptance run and by `migrate`.

If this host has `EnableLUA=0`, start there — there is no medium-integrity
broker on such a machine, so nothing below can prove anything:

```
python Scripts\secure_apps\sa_cli.py privileged-helper prepare-uac --confirm
(reboot)
```

Then install the silent elevation mechanism, once:

```
python Scripts\secure_apps\sa_cli.py privileged-helper install
python Scripts\secure_apps\sa_cli.py privileged-helper check --start
```

`check --start` must end with `SILENT_PRIVILEGED_START_ACCEPTED`: the task is
valid, the helper started without a dialog, it arrived at high integrity, and
this process stayed medium. From here on, opening and locking a vault raise no
UAC prompt.

Before anything else, check what this host can actually do — an old
`webauthn.dll` and a non-elevated HID path both look like "no key" if you do
not ask:

```
python tests\secure_apps_interactive.py --probe
```

On a Windows 10 host expect `webauthn.dll` API version 2 and transport
`ELEVATED_CTAP_HELPER`. That is the supported configuration, not a failure.

Then prove the hardware, once, before any real data is involved (security
key, PIN, touch, one unplug, one workstation lock; nothing configured is
touched, and the disposable container is deleted at the end):

```
python tests\secure_apps_interactive.py --disposable-acceptance
```

Every check must print `PASS`. A `SKIP` is not a pass — it writes a record
that authorises nothing. See [Hardware acceptance](#hardware-acceptance).

1. Import the portable application into managed storage:

   ```
   python Scripts\secure_apps\sa_cli.py import-app obsidian --source "C:\PortableApps\Obsidian"
   ```

   The executable is not encrypted and does not need to be. What the
   import buys is **ownership**: afterwards the profile launches a copy
   SAITULS placed, so an update or a replacement elsewhere on disk
   cannot silently become what the protected launch runs. To launch the
   original portable copy instead, set
   `application.allow_unmanaged_executable: true` and point
   `application.executable` at it -- an explicit, written-down decision,
   which is why it is not the default.
2. SAITULS → Tools → **Secure Apps** → **Manage key** → *Create vault +
   enroll first key*. Store the recovery material when it is shown; it is
   shown once.

   Enrollment builds the container at `<container dir>\_enrollment_mount`
   and never at the profile's `mount_path`, so it runs happily while the
   plaintext vault is still sitting at that path. When it finishes, the
   volume is locked and detached (`ENROLLMENT_READY`) and the plaintext has
   not been touched.
3. Check the gate before moving a byte — it needs no key, mounts nothing
   and copies nothing:

   ```
   python Scripts\secure_apps\sa_cli.py migrate obsidian --check-acceptance
   ```

   `MIGRATION_READY` is the go-ahead. Anything else names its own reason;
   note that enrollment itself does not change the profile's security
   configuration, so a record made before it stays valid.
4. Migrate the plaintext vault (CLI above). It copies from the plaintext
   path into a staging mount, verifies, relocks, re-verifies, and only then
   renames the plaintext aside and mounts the encrypted volume at the real
   path. Verify, then archive or delete the reported plaintext copy
   **yourself**.
5. **Open** launches the application; **Lock now** relocks immediately.

### Confirming there are no runtime prompts

Once, by hand, after setup. The point of the whole mechanism is a negative
result, and a negative result has to be looked at:

```
 1. close every Secure Apps process
 2. Open Obsidian through Secure Apps
 3. FIDO2 authentication happens          (PIN / touch: expected)
 4. NO Windows UAC prompt appears         <- the property under test
 5. close Obsidian
 6. the vault disappears
 7. NO Windows UAC prompt appears
 8. reopen in Default mode
 9. no FIDO2 prompt inside the cached session
10. no UAC prompt
11. switch to Aggressive
12. close and reopen
13. FIDO2 authentication is required again
14. no UAC prompt
15. lock Windows (Win+L)
16. the protected storage relocks, with no UAC prompt
```

The evidence this is meant to produce:

```
BROKER_MEDIUM_INTEGRITY    HELPER_HIGH_INTEGRITY
SILENT_PRIVILEGED_START_ACCEPTED
FIDO2_HARDWARE_ACCEPTED    STORAGE_ACCEPTED
NO_RUNTIME_UAC_PROMPTS     MIGRATION_READY
```

A FIDO2 PIN prompt and a Windows consent dialog are not the same thing and
are not traded against each other: the first is user verification for the key
that decrypts the vault, and it stays. The second is Windows privilege
elevation, and after setup there is none.

### Telling the application which vault to open

`application.vault_argument_style` decides what is appended to the
argument list once the filesystem is mounted:

| value | appended |
|---|---|
| `path` (default) | the mount path, as one argument |
| `obsidian-uri` | `obsidian://open?path=<urlencoded mount path>` |
| `none` | nothing; the profile's own `arguments` are the whole story |

The URI form is built **only after** the volume is mounted, and it is
passed to the profile's own executable -- never handed to the shell. A
globally registered `obsidian://` handler therefore cannot redirect a
protected launch to a different Obsidian. If the bare-path form does not
open the vault on your build, switch the profile to `obsidian-uri`.

The `Manage profile` dialog is read-only on purpose: the registry is the
authority and is validated fail-closed on load, so a GUI that could write a
policy field would have to reimplement every one of those checks.
