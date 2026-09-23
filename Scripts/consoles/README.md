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

## Explorer menu

```
powershell -ExecutionPolicy Bypass -File .\Add-AgentConsolesMenu.ps1
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_CONSOLES_MENU.PS1 -VerifyOnly
powershell -ExecutionPolicy Bypass -File .\Remove-AgentConsolesMenu.ps1
```

One cascade ("Открыть в", from `menu_label`) on folders, folder backgrounds
and drives, one item per enabled profile in file order. Per-user (HKCU), no
elevation. Add, remove or reorder a profile in `consoles.json` and rerun the
installer.

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
unknown `working_directory_mode` or a forbidden environment key all refuse
the whole registry.

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
