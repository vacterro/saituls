# SAITULS documentation

The root `README.md` is the user guide and the source of truth for installation,
every shipped feature, optional agent menus, dependencies, removal and developer
checks. Start there; this folder holds narrower technical references.

## Reference files

- `INSTALL.md` — manual install/uninstall and per-file registry import.
- `REGISTRY.md` — exact 14-feature registry catalog and encoding rules.
- `SCRIPTS.md` — worker behavior, arguments and safety notes.

## Repository map

| Path | Purpose |
|---|---|
| `INSTALL.cmd` / `setup.ps1` | One-click install/repair entry and implementation |
| `SAITULS.cs` / `SAITULS.exe` | Main control-panel source and ready-to-run binary |
| `Registry/` | 14 install files plus matching removal files |
| `Scripts/` | Batch, Python and PowerShell workers |
| `Bin/` | Downloaded media runtime payload; heavy binaries are release assets, not Git source |
| `Installers/` | Manual and optional agent-menu installers |
| `i18n/` | 33 string bundles and registry-locale generator |

The agent quota monitor **LIMISAW** was extracted from this repository into its
own project; nothing here depends on it any more.

The personal `___AHK/` directory and `.saipen/` project memory are intentionally
ignored by Git and are not part of the public release.
