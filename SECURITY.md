# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Instead report
privately so the issue can be fixed before disclosure.

Preferred: open a private report via
[GitHub Security Advisories](https://github.com/vacterro/saituls/security/advisories/new).

If that is not possible, send details to the repository owner through a
private channel.

## Scope

- Registry import handlers (`Registry/`, `Installers/`)
- Launchers that spawn elevated processes (`SAITULS_LAUNCHER.cmd`,
  `Scripts/AI_AGENT_LAUNCHER.PS1`, `Installers/INSTALL_*.PS1`)
- Anything reading or writing user credentials (Codex launchers)
- Anything that can terminate a process (`Scripts/saispin_watch.ps1` and its
  scheduled task)

## What to include

- Product, version and where it runs (Windows 10/11 x64)
- Steps to reproduce
- Impact
- Suggested fix, if known

## Response

The owner will acknowledge reports within a few days and aims to fix
confirmed issues promptly.
