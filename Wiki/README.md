# SAITULS documentation

The root `README.md` is the user guide and the source of truth for installation,
every shipped feature, optional agent menus, dependencies, removal and developer
checks. Start there; this folder holds narrower technical references.

## Reference files

- `INSTALL.md` — manual install/uninstall, locale generations, optional menus and safe per-file registry import.
- `REGISTRY.md` — exact 14-feature registry catalog, encoding and tokenization rules.
- `SCRIPTS.md` — workers, subsystem boundaries, commands, safety and recovery behavior.
- `EXCEPT_PASS_CLASSIFICATION.md` — reviewed Python exception-suppression sites and the reasons they are intentional or repaired.

## Repository map

| Path | Purpose |
|---|---|
| `INSTALL.cmd` / `setup.ps1` | One-click install/repair entry and implementation |
| `SAITULS.cs` / `SAITULS.exe` | Main control-panel source and ready-to-run binary |
| `VERSION` | Current tree version (`0.2.0`) |
| `Registry/` | 14 install files plus matching removal files and the legacy FFmpeg remover |
| `Scripts/` | Context-menu workers, shared safety primitives, payload helpers and subsystem launchers |
| `Scripts/accounts/` | SAI Accounts control plane: registry, broker, background keeper task installer and engine |
| `Scripts/taskbar_edge/` | Non-elevated, per-session TaskbarEdge reveal helper and its C# source |
| `Scripts/secure_apps/` | FIDO2-gated application vault: broker, auth, storage, migration and pinned privileged runtime |
| `Scripts/saipatch/` | OpenCode patch manager, Queue Viewer, Clipboard+ and item-list tools |
| `Scripts/scenarios/` | Data-driven migrated media/clean/backup utility launcher |
| `Scripts/consoles/` | Data-driven, non-elevated agent-console launcher and Explorer cascade registry |
| `Scripts/shell_doctor/` | Read-only Explorer/Start-lag evidence collector and findings engine |
| `Scripts/saiopt/` | Tray resident optimizer and read-only disk/shell watchers |
| `Scripts/_other/` | Standalone WhatsApp maintenance scripts outside the default install set |
| `Bin/` | Downloaded media runtime, legacy GUI payload and the shared `FFMPEG_RUN.PS1` runner |
| `Installers/` | Core install, OpenCode/Cline menus, Codex cascade and agent-console menu installers |
| `Add-CodexContextMenus.ps1` / `Remove-CodexContextMenus.ps1` | Machine-wide Codex menu transaction |
| `Add-AgentConsolesMenu.ps1` / `Remove-AgentConsolesMenu.ps1` | Thin wrappers around the per-user agent-console cascade installer |
| `i18n/` | 33 locale string bundles and fail-closed registry-locale generator |
| `Wiki/` | These five maintained reference pages |
| `tests/` | Registry, installer, destructive-worker, subsystem and SAI Accounts keeper verification harnesses |
| `9router_scanner/` | Standalone 9Router model scanner and AgentRouter integration suite; not wired into SAITULS Tools |

LIMISAW and Problip were extracted from this repository. Neither ships here,
and no live SAITULS path depends on either project.

The personal `___AHK/` directory and `.saipen/` project memory are intentionally
not part of the public release.
