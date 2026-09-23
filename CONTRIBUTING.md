# Contributing to SAITULS

Thanks for helping. This is a personal toolkit that grew into a public repo,
so please keep changes small, verified and free of machine-specific data.

## Ground rules

- **No personal paths.** Never commit `C:\Users\...`, `V:\...`, drive letters or
  machine-specific registry entries. Use `%%ROOT%%` tokens or `%USERPROFILE%`
  placeholders.
- **No secrets.** Never commit `auth.json`, tokens, API keys or `.ini` files
  that carry them. `SAITULS.ini` is gitignored for this
  reason, and so are `saispin_state.json` / `saispin.log`, which record the
  command lines of whatever was running on the machine, plus
  `saispin_notify.json`, which records which process identities have already
  been reported.
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
| SAISPIN safety rules | `Scripts/saispin_logic.py` — the pure decision engine; no Windows API belongs in it |
| SAISPIN sampling / kill path | `Scripts/saispin_watch.ps1`, and `Scripts/Install-SaispinTask.ps1` for the scheduled task |
| Installer | `INSTALL.cmd`, `setup.ps1`, `Installers/INSTALL_ALL.PS1` |
| Translations | `i18n/strings/*.json`, `i18n/docs/*/`, `i18n/tools/gen_locale_reg.py` |
| Docs | `README.md`, `Wiki/`, `CHANGELOG.md` |

The agent quota monitor **LIMISAW used to live here** and is now its own
project. Nothing in this repository depends on it.

## Pull request checklist

- [ ] `python tests\test_regs.py` passes (0 failures)
- [ ] `python tests\test_workers.py` passes when a destructive worker changed
      (0 failures) — these commands bypass the Recycle Bin, so a regression here
      is unrecoverable user data; the suite mutates the filesystem from inside
      the confirmation dialog because that is where the real race lives
- [ ] `python tests\test_saispin.py` passes when any SAISPIN rule changed
      (0 failures) — this is the suite that proves a process cannot be killed
      without orphanhood *and* sustained spin *and* an explicit arming flag
- [ ] `powershell -File tests\test_locale_generation.ps1` passes when
      `INSTALL_ALL.PS1` changed (0 failures); it runs the real installer in a
      throwaway tree with `reg.exe` recorded, so a locale switch that layers two
      menu trees is caught before it reaches a registry
- [ ] `powershell -File tests\test_codex_menu_preflight.ps1` passes when a
      context-menu installer changed (0 failures); it proves a refused install
      leaves the existing menu value-equivalent, against a throwaway `HKCU` key
- [ ] `powershell -File tests\test_shell_menu.ps1` passes when a `.REG` command
      line or a `Scripts\*.PYW` argument changed (0 failures) — a shell verb
      resolves a bare executable only from the Windows directory and `System32`,
      so `pythonw.exe` on `PATH` is not launchable from a menu
- [ ] `powershell -File tests\test_import_safe.ps1` passes when
      `Registry\IMPORT_SAFE.PS1` changed (0 failures); `reg.exe` exits 0 even
      when it skipped an unparseable value line, so the value must be read back
- [ ] `powershell -File tests\test_self_elevation_result.ps1` passes when a
      script that relaunches itself as administrator changed (0 failures); the
      unelevated parent can only observe the launch, so it must wait for the
      elevated child and exit with that child's code — otherwise a cancelled UAC
      prompt is indistinguishable from a completed registry change
- [ ] `Scripts\saispin_watch.ps1 -SelfTest` passes on **both** Windows
      PowerShell 5.1 and PowerShell 7 when the watcher changed (0 failures);
      5.1 is where the pipe-encoding and automatic-variable traps live
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
  The substituted root must have its backslashes doubled: inside quoted registry
  value data a backslash is an escape character, and `reg.exe` silently skips a
  line it cannot parse while still exiting 0.
- A menu command line must not start with a bare interpreter name. A shell verb
  resolves an unqualified executable only from the Windows directory and
  `System32`, so the workers are launched with `pyw.exe`, not `pythonw.exe`.
- A worker's target comes from `Scripts/shell_arg.py`. Explorer passes `"%1"`
  literally, so a drive root arrives with its backslash escaping the quote and a
  naive `.strip('"')` yields a drive-relative path.
