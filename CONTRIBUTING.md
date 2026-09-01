# Contributing to SAITULS

Thanks for helping. This is a personal toolkit that grew into a public repo,
so please keep changes small, verified and free of machine-specific data.

## Ground rules

- **No personal paths.** Never commit `C:\Users\...`, `V:\...`, drive letters or
  machine-specific registry entries. Use `%%ROOT%%` tokens or `%USERPROFILE%`
  placeholders.
- **No secrets.** Never commit `auth.json`, tokens, API keys or `.ini` files
  that carry them. `SAITULS.ini`, `LIMISAW.ini` and `problip.ini` are gitignored
  for this reason.
- **Keep English.** README, Wiki and code comments are English only.
- **Verify before you push.** Run `python tests\test_regs.py` and make sure it
  passes with zero failures.

## Setup

```cmd
python tests\test_regs.py
```

Build the WinForms apps with the built-in .NET Framework 4.x compiler — see the
README "For developers" section for the exact `csc.exe` commands.

## What to change where

| Change | Where |
|---|---|
| Explorer menu behaviour | `Registry/*.REG` + `Scripts/` workers |
| SAITULS GUI | `SAITULS.cs` (compile with `/win32icon:SAITULS.ico`) |
| LIMISAW monitor | `LIMISAW.cs` (compile with `/win32icon:heh.ico`) |
| Problip monitor | `problip/Problip.cs` (compile with `/win32icon:problip\problip.ico`) |
| Installer | `INSTALL.cmd`, `setup.ps1`, `Installers/INSTALL_ALL.PS1` |
| Translations | `i18n/strings/*.json`, `i18n/docs/*/`, `i18n/tools/gen_locale_reg.py` |
| Docs | `README.md`, `Wiki/`, `CHANGELOG.md` |

## Pull request checklist

- [ ] `python tests\test_regs.py` passes (0 failures)
- [ ] `.ps1` / `.cmd` / `.PYW` files parse cleanly
- [ ] No personal paths, no secrets, no machine-specific data
- [ ] README / Wiki updated if user-facing behaviour changed
- [ ] CHANGELOG.md updated for user-facing changes

## Notes

- Heavy binaries (ffmpeg, yt-dlp, AV1/RTX40 builds, Ghostscript, ExifCleaner)
  are release assets, not Git source. A full local build needs the payload zip
  extracted into `Bin\` / `Bin\App\`.
- Context-menu `.reg` files are `%%ROOT%%`-tokenized — always import through
  `Registry/IMPORT_SAFE.PS1` or `INSTALL_ALL.PS1`, never raw `reg import`.
