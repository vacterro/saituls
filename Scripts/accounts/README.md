# SAI Accounts (control plane)

One canonical multi-account registry for the SAI ecosystem. The contract of
record is [SAI_ACCOUNTS.md](SAI_ACCOUNTS.md); this README covers layout,
build, deployment, and the day-one operator surface.

## What lives here

| File | Role |
|---|---|
| `SaiAccounts.cs` | The whole engine in one C# 5 source: registry, CLI, per-context broker |
| `SaiAntigravityShim.cs` | `sai-antigravity.exe` compat shim: forwards only `usage <ref>` to the engine and maps the response to the legacy envelope |
| `build.ps1` | Compiles the engine, helper and compatibility shim with Windows .NET Framework `csc` (no SDK needed) |
| `Install-SaiAccountsKeeper.ps1` | Registers the one canonical `SAITULS\SAI Accounts Keeper` scheduled task (logon + every 2 min, installed engine only, no account state) |
| `bin/` | Build output (gitignored) |
| `../../tests/test_sai_accounts.py` | Isolated registry, migration, broker and shim harness |
| `../../tests/test_sai_account_selectors.py` | Selector ambiguity, stable targeting and explicit binding validation |
| `../../tests/test_accounts_build.ps1` | Build ownership: a test/source build preserves installed brokers |

## Build

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\accounts\build.ps1
```

Missing compiler is FAIL, never SKIP. Smoke run (`list`) is part of the build.

## Deploy (canonical locations)

Building is not deploying. Prefer the build script's `-Deploy` switch:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\accounts\build.ps1 -Deploy
```

It warns when a broker is still running, because that process keeps the old
binary until it is restarted — a deployed file does not replace a loaded image.
A plain build prints `NOT deployed` rather than letting a fixed binary sit in
the repo looking finished while the machine runs the old one. Not
hypothetical: the broker-loop fix documented in `Wiki/ACCOUNTS-STATUS.md`
section 8 was built, tested and fully green, and the error still kept
appearing for hours because the copy under `ProgramData` was six hours stale.

The equivalent manual copy, for reference:

```powershell
Copy-Item Scripts\accounts\bin\sai-accounts.exe      C:\ProgramData\SAI\Accounts\bin\   -Force
Copy-Item Scripts\accounts\bin\sai-antigravity.exe   C:\ProgramData\SAI\Antigravity\bin\ -Force
```

`C:\ProgramData\SAI\Accounts\bin\sai-accounts.exe` is the path every consumer
resolves — SAITULS tries it before the repo copy, deliberately, so a stale
source-tree binary can never shadow the installed plane. The shim goes where
legacy consumers already look
(`C:\ProgramData\SAI\Antigravity\bin\sai-antigravity.exe`) and finds the
engine beside itself, in the canonical location, or on PATH.

## CLI surface (summary)

```
sai-accounts list [--all]            # active accounts (ARCHIVED only with --all)
sai-accounts get <ref>               # one account descriptor
sai-accounts snapshot                # batched state for all active accounts
sai-accounts status <ref>            # context/auth state; use usage for quota
sai-accounts usage <ref>             # quota windows (current or via broker)
sai-accounts launch <ref> --workdir <dir>  # open the account's console
sai-accounts import                  # one-shot migration from legacy files
sai-accounts rename <ref> <name>     # display alias (identity never changes)
sai-accounts move <ref> up|down      # canonical order; labels stay stable
sai-accounts bind-identity <ref> --clear # clear a binding, never a credential
sai-accounts set-state <ref> <s>     # ENABLED | DISABLED | FROZEN | ARCHIVED
sai-accounts set-hidden <ref> <true|false> # presentation-only visibility
sai-accounts context start|stop <ref> # manage the selected Windows broker
sai-accounts sync-binary             # refresh the shared provider binary
sai-accounts doctor                  # environment checks
sai-accounts capabilities <provider> # quota/multi-account capability flags
```

Use the immutable `account_id` in application integrations. For a human,
`AG1`/`CL1`/`CX1` are stable compact selectors and remain correct after rename
or reorder. Duplicate aliases are allowed for display, but a command using
an ambiguous alias is refused with the candidate ids. A matching email label
never proves that two provider identities are equal. Labels are generated at
read time from the persisted provider ordinal and optional display alias.

Building to a separate output directory stops only brokers running that exact
output image. It preserves the installed control plane and other isolated
builds. Deployment stops only brokers of the exact installed engine image;
console title guards and brokers of other builds keep running. Deployment
remains an explicit `-Deploy` action.

Federated Claude and Codex consoles retain their stable compact account label
in both the console title and native window title. A bounded 50ms guard repairs
vendor title changes and keeps the window's canonical account property. It
avoids rewriting titles that already match.

New native consoles normalize an absent or `dumb` inherited `TERM` to
`xterm-256color`, so headless callers cannot strand Codex at its terminal
confirmation. A custom `TERM` and the parent's environment are preserved.

## Background keeper

Managed brokers outlive the SAITULS window, but nothing reconciled them after
it closed. The keeper is one Task Scheduler task, `SAITULS\SAI Accounts Keeper`,
running the installed engine as `sai-accounts autoconnect --budget-ms 90000` at
logon and about every two minutes. The action runs a fixed
`sai-accounts-keeper.vbs` under `wscript.exe`, so the console engine is created
with `SW_HIDE` and no console window is ever mapped (a PowerShell
`-WindowStyle Hidden` host still flashed a visible console and stole the
foreground every cadence). It owns no account state: it restores only
`ENABLED` managed accounts with `auto_connect == true`, never opens an IDE
window, and races the live GUI timer harmlessly (engine sweep mutex +
`deduplicated` loser). The SAITULS Accounts Advanced panel holds the global
toggle — distinct from each account's Auto-connect — with actual task-state
verification, last-check reporting, and Repair. Registering the task needs
elevation on a locked-down box; the toggle retries elevated via the standard
RunAs pattern and otherwise degrades to a warning.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\accounts\Install-SaiAccountsKeeper.ps1 -Enable
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\accounts\Install-SaiAccountsKeeper.ps1 -VerifyOnly
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\accounts\Install-SaiAccountsKeeper.ps1 -Disable
```

## Broker

Secondary Windows contexts run one broker each
(`sai-accounts --broker <expected-sid> <scope> <operator-sid>`), started
through the operator's runas flow. The pipe is
`SAI.Accounts.<scope>.<context-fingerprint>` with an ACL of exactly the
target context SID, SYSTEM, and the operator SID. Allowlisted ops only:
`status`, `usage`, `launch` (workdir only), `auth`, `logout`, `shutdown`. Requests are one JSON
line, bounded at 8 KiB; workdir availability fails closed. No
`/savecred`, no `/netonly`, no arbitrary command execution, ever.

## Security rules (extract)

* Never store or log Windows/Google passwords, OAuth tokens, session cookies,
  passkey material, Credential Manager blob bytes, or authorization codes.
* Credential checks report presence and last-written time only.
* Secret-shaped fields in the registry are rejected at parse time.
* Identity and workdir fail closed.

The full list is in [SAI_ACCOUNTS.md](SAI_ACCOUNTS.md) (authoritative).

## Tests

Managed Antigravity consoles enable the native Ctrl-key shortcuts
(`CtrlKeyShortcutsDisabled=0`) and mirror the operator's installed input
languages and switching hotkeys into their own Windows profile. The helper
copies only validated layout/language values and its fixed console settings.
The settings are refreshed before each managed console launch, even when the
account is already connected. If the hive was unavailable before sign-in,
the helper retries after Windows loads the profile. Per-executable overrides
live below `Console`, and the launch result reports any preparation failure.
It adds no global keyboard hook or replacement clipboard mechanism.

```powershell
python tests\test_sai_accounts.py
python tests\test_managed_console_context.py
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_console_profile.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_managed_console_launch.ps1
python tests\test_sai_account_selectors.py
python tests\test_account_console_titles.py
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_account_console_environment.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_account_identity.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_accounts_build.ps1 -Deploy
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_accounts_runtime.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_accounts_tab.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_accounts_keeper.ps1
```

These are hermetic: they run against an isolated `SAI_ACCOUNTS_ROOT` and never
touch the live `C:\ProgramData\SAI\Accounts` registry or the legacy input files.
They are green on a machine where managed AG2 is not actually signed in, because
they prove behaviour against a stand-in engine, not against a real vendor login.

For the real thing — the installed plane, the real managed Windows account:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\accounts\AG2_ACCEPTANCE.PS1
```

That script mutates live connection state (it connects, disconnects, restarts
AG2's broker and launches `agy.exe` in the current directory), so it is the
operator's to run, not CI's. Run `build.ps1 -Deploy` first: it refuses to report
green against binaries that are not the ones in this repository. Its section D
is the one that answers "is AG2 a second human or a second login?".
