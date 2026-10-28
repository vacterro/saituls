# AI consoles

A data-driven launcher for interactive agent CLIs. Separate from Secure Apps
on purpose: these profiles are **not** behind the encrypted vault, and none
of them requires authentication.

SAITULS → Tools → **AI Consoles** opens a picker; picking a profile asks for
a project folder and opens a fresh console there.

## Profiles

Every profile starts in its CLI's permission-skipping ("YOLO") mode.

| id | command | child environment |
|---|---|---|
| `claude-1` | `claude --dangerously-skip-permissions` | `CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude` |
| `claude-2` | `claude --dangerously-skip-permissions` | `CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude-account2` |
| `codex-1` | `codex --dangerously-bypass-approvals-and-sandbox` | `CODEX_HOME=%USERPROFILE%\.codex` |
| `codex-2` | `codex --dangerously-bypass-approvals-and-sandbox` | `CODEX_HOME=%USERPROFILE%\.codex-account2`, API keys removed |
| `codex-3-free` | `codex --dangerously-bypass-approvals-and-sandbox` | `CODEX_HOME=%USERPROFILE%\.codex-account3free`, API keys removed |
| `antigravity` | `agy --dangerously-skip-permissions` | – |
| `zcode` | `node {script} --cwd {workdir} --mode yolo` | – |
| `cline` | `cline --cwd {workdir} --auto-approve true --tui` | – |
| `opencode` | `opencode {workdir} --auto` | – |

`{script}` is the first existing path in the profile's `script_candidates`.
ZCode's desktop app ships only the headless part of its CLI (its bundled
`zcode.cjs` fails with `Cannot find package '@zcode/tui'`), so the profile
needs a full ZCode CLI build with a built `@zcode/tui`: set `ZCODE_CLI` to its
`dist\zcode.cjs`, or keep the checkout path listed in `consoles.json`.

## Shared accounts (optional)

SAI Accounts is an **optional** control plane, and this subsystem works with
or without it. Nothing here requires it, and nothing here has to be installed
for any other SAI application to be usable.

When `sai-accounts.exe` is installed, the three profiles marked
`expand_accounts` (`shared-claude`, `shared-codex`, `shared-antigravity`)
render one console per canonical account, and those consoles launch through
the control plane:

```
sai-accounts.exe launch <canonical-account-id> --workdir <workdir>
```

The engine owns the identity, the child environment, the Windows execution
context and the lifecycle check. A profile that expands carries **no command
at all** — the executable comes from the engine resolver, never from
`consoles.json`, so editing the registry can never choose what gets run. A
template that declares a `command` is refused at validation.

A canonical account that points at the same profile directory as a local
profile — or, for Antigravity, the same Windows user — is the *same account*,
and is shown once, with the canonical identity. Matching runs on provider plus
locator; a display name is never a matching key. `zcode`, `cline` and
`opencode` are tools, not accounts, and are never touched by this.

An account that is globally hidden, disabled, frozen or archived leaves the
launch surface. It still exists, and the SAITULS **Accounts** tab still shows
it; a menu entry that can only fail is worse than no entry.

Force either mode with `-Federation auto|on|off` on `CONSOLES.ps1` and on
`INSTALL_CONSOLES_MENU.PS1`. `off` is how the standalone suite runs on a
machine that does have the control plane.

## Explorer menu

```
powershell -ExecutionPolicy Bypass -File .\Add-AgentConsolesMenu.ps1
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_CONSOLES_MENU.PS1 -VerifyOnly
powershell -ExecutionPolicy Bypass -File .\Remove-AgentConsolesMenu.ps1
```

One cascade on folders, folder backgrounds and drives, one item per enabled
profile in file order. Per-user (HKCU), no elevation. The parent label comes
from the translation bundle — `i18n\strings\strings.<locale>.json`, key
`menu.open_in` — so it follows the machine's UI language instead of being one
hardcoded string. It used to be a Russian label in `consoles.json`, which put
a Russian parent over translated children in every other locale.

The item list is built from the same expansion the launcher uses
(`ACCOUNTS.ps1`), so the menu and the picker cannot disagree. Adding, removing
or reordering a profile in `consoles.json` still means rerunning the
installer — and so does changing an account in SAI Accounts, which is why the
Accounts tab reruns it for you after any rename, hide, state change,
re-authentication or import, why SAITULS verifies the cascade at startup and
repairs drift on its own, and why a failed update is reported as
`EXPLORER MENU UPDATE FAILED` with a **Repair menu** button rather than being
left silently stale. An account that leaves the launch surface (deleted,
hidden, disabled, frozen, archived) also takes a local profile that collapsed
into it, so the same physical account is never still on offer as a local
console.

`-VerifyOnly` compares every installed verb against the current launcher path
and says which path the stale verb points at, which is what makes a repository
that moved a one-line repair instead of a mystery click that does nothing.

## When a launch fails

A click in Explorer has no terminal to read, so the launcher never relies on
one. Every Explorer invocation runs with `-FromExplorer`, which arms a launch
boundary: the click ends in either **LAUNCHED** or a durable failure — a dialog
naming the profile, the account, a machine-readable category and the reason,
plus one sanitized line in a bounded log:

```
%LOCALAPPDATA%\SAITULS\logs\consoles-launch.log
```

Each line carries the timestamp, profile id, canonical account id, working
directory, launcher and engine paths, the engine's structured status, the exit
code, the spawned pid, the failure category and the elapsed time. Rotation
keeps one previous file at 512 KB. No environment map, credential, token or
child command line is written, and a secret-shaped assignment inside a reason is
masked before it is. Before this existed, any preflight error — a stale profile
id, a missing CLI, an unreadable project folder, an engine that refused the
launch — printed into a console that was already closing: the operator saw a
black window flash and nothing else.

Categories worth recognising:

| Category | Meaning |
| --- | --- |
| `LAUNCHED` | the profile's console, or the account's own console, was started |
| `STALE_EXPLORER_MENU_ENTRY` | the verb names a profile the cascade should no longer list; rebuild the menu |
| `PROFILE_UNKNOWN`, `PROFILE_DISABLED` | the registry and the installed menu disagree |
| `SCRIPT_PREFLIGHT_EXCEPTION` | anything before the child existed: missing folder, missing CLI, malformed registry |
| `ACCOUNT_LAUNCH_FAILED` | the engine answered a status other than `launched`; the reason carries it verbatim |
| `ACCOUNT_ENGINE_NO_ENVELOPE` / `_SCHEMA_MISMATCH` / `_INCONSISTENT` / `_TIMEOUT` | no envelope, a foreign envelope, or an engine that said `launched` and then exited nonzero |

A federated entry is installed with `-WindowStyle Hidden`: the account's console
is opened by the engine in the account's own Windows context, so the PowerShell
host underneath it is a disposable bootstrap. Starting it hidden is why a
federated click no longer produces a black window and then a second one — and
failures still reach the operator, because the dialog is raised by the boundary,
not by the console that is about to close.

Federated success is decided by the engine's own envelope
(`sai.accounts/launch/1`, `status == "launched"`), never by its exit code alone.
A valid JSON response is not a successful launch: an older or vendored engine
that answers `launch_failed` and still exits 0 fails here too.

## Not elevated

The Explorer-menu OpenCode/Cline launcher (`Scripts\AI_AGENT_LAUNCHER.PS1`)
self-elevates to maximum privilege. **This subsystem does not inherit that**,
and it is not an oversight: an autonomous AI console that silently starts as
Administrator is a far larger blast radius than the convenience is worth.

There is no `-Verb RunAs` anywhere in `CONSOLES.ps1`. A future profile may
declare `requires_admin`, and then the launcher *refuses to start* unless it
is already elevated — it still never elevates itself. No shipped profile
declares it.

The existing OpenCode/Cline path is untouched.

## What isolation actually means

`CLAUDE_CONFIG_DIR` separates the two accounts' configuration and credential
directories, and that is the whole of the claim. Upstream Claude Code may
still discover user-level instructions from paths **outside**
`CLAUDE_CONFIG_DIR` — a global `CLAUDE.md`, for instance. The two profiles
are isolated at launcher level, not sandboxed from each other. Do not rely on
this for anything stronger than keeping two accounts' sessions apart.

`HOME` is never set. It is on the launcher's refused-keys list along with
`USERPROFILE`, `PATH`, `APPDATA`, `LOCALAPPDATA`, `TEMP`, `TMP`, `COMSPEC`,
`SYSTEMROOT`, `WINDIR` and `PATHEXT`: overriding any of them moves far more
than an agent's configuration.

The SAITULS process environment is never modified. Variables are set in the
short-lived console host that exists only to run that one agent.

## Registry

`consoles.json`, schema `saituls.consoles/1`. A profile declares a **bare
command name** and an **argument list** — never a shell string, so nothing in
this file can become a command. Validation is fail-closed: unknown schema,
a command containing a path separator or a space, a non-string argument, an
unknown `working_directory_mode`, a forbidden environment key, or an account
template that smuggles in a command or an argument list all refuse the whole
registry.

Two optional fields carry the federation contract: `provider` declares which
local identity a profile already claims, and `expand_accounts` marks a
template. Neither changes what a local profile does.

## Console behaviour

* the command is resolved on PATH **before** launching, so a missing CLI
  produces a dependency error naming the fix instead of a window that flashes
  and disappears;
* the working directory is asked for (`working_directory_mode: ask`);
* the title is project-first, `<project> | <profile> | <path>`, capped below
  the 255-character portable OSC ceiling, and held by a guard timer — the
  same contract as the Explorer-menu launcher, restated here rather than
  shared because that file carries the self-elevation block this subsystem
  must not inherit;
* Ctrl+C keeps interrupting the agent (`ENABLE_PROCESSED_INPUT`) and
  Ctrl+V / right-click keep pasting (`ENABLE_QUICK_EDIT_MODE`,
  `ENABLE_INSERT_MODE`, `ENABLE_EXTENDED_FLAGS`). The launcher installs no
  Ctrl+C handler of its own, so the agent owns the signal;
* a nonzero exit keeps the window open and says what happened.

## Probe

```
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\consoles\CONSOLES.ps1 -SelfTest
powershell -NoProfile -ExecutionPolicy Bypass -File Scripts\consoles\CONSOLES.ps1 -SelfTest -Profile claude-2 -WorkDir C:\some\project
```

Reports the resolved command, working directory, title, project-first flag,
argument list, elevation flags and non-secret environment metadata. Launches
nothing and sets no environment variable.

```
python tests\test_consoles_launcher.py
```
