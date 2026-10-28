# Install / uninstall

## Recommended one-click install

Double-click `INSTALL.cmd` at the repository root. It calls `setup.ps1`, which:

1. self-elevates once;
2. installs missing Python 3.13 and the Windows `pyw.exe` launcher;
3. checks/installs Clipboard+ dependencies (`pywin32`, Pillow) by import probe;
4. validates and repairs `SAITULS.exe` when its source, PE header or version is stale;
5. downloads missing media tools only after staging and validating them;
6. refreshes the 14 default Explorer registry features;
7. starts SAITULS without carrying the setup administrator token.

Re-run `INSTALL.cmd` to repair a moved or incomplete installation. The setup
mutex prevents concurrent repairs from sharing downloads. A non-zero result or
a named failure is reported; setup does not print success for a failed child.

Useful switches:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -SkipPayload
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -NoLaunch -WaitAtEnd
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -EnableAutostart
```

`-SkipPayload` skips media download/repair. `-NoLaunch` leaves SAITULS closed.
`-EnableAutostart` updates both the INI setting and its projected per-user Run
entry.

## Full default install

```powershell
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_ALL.PS1
```

`INSTALL_ALL.PS1` imports every active `Registry\*.REG` except `*_REM*`,
`*_UTF16*`, `*_REWRITTEN*` and `FFMPEG.REG`: 14 install files. It substitutes
`%%ROOT%%` in a temporary copy, imports through `reg.exe`, and checks the child
exit code. The path-bearing registry files are relocation-proof, but a raw
`reg import` bypasses the substitution and creates broken commands.

The installer takes `Global\SAITULS_REGISTRY_MUTATION`. It resolves source
files, recorded-generation manifests, launchers and required CLIs before the
first mutation, snapshots managed registry keys plus `Installers\.installed_lang`,
then rolls both back on an import or marker failure. A busy lock is reported
without mutation.

## Locale install and uninstall

```powershell
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_ALL.PS1 -Lang et
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_ALL.PS1 -Uninstall
```

`-Lang` uses a generated `i18n\reg\<locale>` generation when it exists. The
English `Registry\` tree is the fallback only when the requested generation is
absent, with a warning. Switching generations removes the previous recorded
generation before applying the new one. The JSON marker
`Installers\.installed_lang` records the applied language and exact source
filenames. Uninstall without `-Lang` removes the recorded generation, not an
assumed English tree; explicit `-Lang` wins and warns on disagreement.

Locale registry files are generated from `i18n\reg-map.json` and
`i18n\tools\gen_locale_reg.py`. The generator is fail-closed and publishes
through a validated temporary directory and atomic replacement:

```powershell
python i18n\tools\gen_locale_reg.py et --out .\i18n\reg\et
```

A missing mandatory label, missing bundle key, unknown argument or publication
failure leaves the previous generation untouched and exits nonzero.

## GUI and selective install

Launch the normal control panel with:

```cmd
SAITULS_LAUNCHER.cmd
```

SAITULS runs as the current user. **Home → Install / repair** runs the full
repair path. **Explorer menus** preflights the selected source and removal
files, takes the shared registry mutation lock, and reports actual elevated
child failures. `Installers\INSTALL_GUI.PS1` provides the standalone selective
WinForms installer for the default registry files; use the documented scripts
when a command-line result or transaction receipt is required.

## Optional OpenCode and Cline menus

The default install does not require OpenCode or Cline. Install their
Explorer menus separately:

```powershell
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_AI_AGENT_MENUS.PS1 -Agent All
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_AI_AGENT_MENUS.PS1 -Agent OpenCode
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_AI_AGENT_MENUS.PS1 -Agent Cline
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_AI_AGENT_MENUS.PS1 -VerifyOnly
```

The entries cover folder, folder-background and drive targets. They use
`Scripts\AI_AGENT_LAUNCHER.PS1`, which self-elevates to Administrator outside
`-SelfTest`, launches OpenCode with `--auto`, and launches Cline with
`--auto-approve true --tui`. The installer preflights all selected CLIs and
snapshots the menu/console state before writing; a failure restores the
pre-run state. The Golden Default console profile and Ctrl+C/Ctrl+V settings
are part of the transaction.

`INSTALL_ALL.PS1 -IncludeAgentMenus` runs the separate AI-agent transaction
after the core generation commits. The separate agent transaction can fail
without claiming that the already-committed core generation rolled back.

Remove only these entries with:

```powershell
powershell -ExecutionPolicy Bypass -File .\Remove-AgentContextMenus.ps1
```

## Optional Codex cascade

The machine-wide Codex cascade is separate from the 14-file generation:

```powershell
powershell -ExecutionPolicy Bypass -File .\Add-CodexContextMenus.ps1
powershell -ExecutionPolicy Bypass -File .\Remove-CodexContextMenus.ps1
```

The cascade uses three isolated Codex account profiles and the reliable
`SubCommands=""` Explorer pattern. It writes `HKLM`, so run it elevated.
The installer transaction snapshots and restores the managed keys. It does
not install the Codex CLI or create accounts.

## Optional agent-console cascade

`Scripts\consoles\consoles.json` is the single profile registry. Install,
verify or remove its per-user Explorer cascade with:

```powershell
powershell -ExecutionPolicy Bypass -File .\Add-AgentConsolesMenu.ps1
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_CONSOLES_MENU.PS1 -VerifyOnly
powershell -ExecutionPolicy Bypass -File .\Remove-AgentConsolesMenu.ps1
```

The ordinary installer removes the older flat OpenCode/Cline entries it
replaces. Pass `-KeepLegacy` to retain them, or invoke
`Installers\INSTALL_CONSOLES_MENU.PS1` with `-RemoveLegacy` only when that
removal is intended. The cascade covers folders, folder backgrounds and
drives, stays `HKCU`, and does not elevate. The machine-wide Codex menu is
still a separate transaction.

## Uninstall and relocation

```powershell
powershell -ExecutionPolicy Bypass -File .\Installers\INSTALL_ALL.PS1 -Uninstall
```

The default uninstall imports removal files and records a clean generation
state; it does not delete source files, payloads or the toolkit folder. It
also invokes the separate AI-agent menu remover because that transaction is
part of the installed optional surface. It does not remove the agent-console
cascade; use `Remove-AgentConsolesMenu.ps1` for that.

Before relocating, close SAITULS and the optional background helpers, move the
entire folder, then run `INSTALL.cmd` again. The `%%ROOT%%` substitution and
generated locale marker update the new location. Do not import tokenized `.reg`
files directly after the move.

## Per-file registry import

> **Warning:** never import a path-bearing `.reg` file with raw `reg import`.
> It writes literal `%%ROOT%%` commands and may still exit zero after skipping
> an unparseable value. Use the safe wrapper:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Registry\IMPORT_SAFE.PS1 -File .\Registry\FFMPEG_MENU.REG
powershell -NoProfile -ExecutionPolicy Bypass -File .\Registry\IMPORT_SAFE.PS1 -File .\Registry\FFMPEG_MENU_REM.REG
```

`IMPORT_SAFE.PS1` detects UTF-16/UTF-8/ANSI input, replaces `%%ROOT%%` with an
escaped root, writes a temporary file, imports it under the shared mutation
lock, reads intended values back and removes the temporary file. Removal files
have no token, but using the wrapper is harmless and keeps the path contract
uniform.
