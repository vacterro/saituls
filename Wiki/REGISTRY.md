# Registry catalog

All context-menu entries live under `Registry/`. The default install set is the
14 files below. `*_REM*`, `*_UTF16*`, `*_REWRITTEN*` and `FFMPEG.REG` are
excluded. Each installed feature has a matching removal file.

| File | Menu label | Hive / key | Function |
|---|---|---|---|
| `COPY_PATH.REG` | COPY PATH | `HKCR\*\shell\CopyPathAsText` | Copy the selected file path as text |
| `DEL_DUP.REG` | DEL DUP | `HKCR\Directory\shell\RemoveDuplicateFiles` | Verify SHA-256 duplicates, then remove redundant copies |
| `DEL_EMPTY.REG` | DEL EMPTY | `HKCR\Directory\shell\DeleteEmptyFolders` | Remove empty directories after confirmation |
| `DEL_JUNK.REG` | DEL JUNK | `HKCR\Directory\shell\DeleteJunkFiles` | Run the selected-folder worker |
| `DEL_JUNK.REG` | DEL JUNK ALL DISKS AVAILABLE | `HKCR\Directory\shell\DeleteJunkFilesAllDisks` | One streaming `SAFE_DISK` pass over every local fixed writable disk (`--all-disks`) |
| `DEL_SAME.REG` | DEL SAME | `HKCR\Directory\shell\RemoveNestedDuplicates` | Flatten nested same-name items within the selected root |
| `DL_YT.REG` | Download From Youtube | `HKLM\SOFTWARE\Classes\Directory\background\shell\DownloadFromYoutube` | Six yt-dlp modes: audio/video, dated or undated, single or playlist |
| `FFMPEG_MENU.REG` | FFmpeg conversion submenu | `HKCU\SOFTWARE\Classes\SystemFileAssociations` | 23 extension roots and 595 explicit registry key headers, all routed through the shared media runner |
| `MERGE_AUD.REG` | MERGE AUD | `HKCR\*\shell\MergeAudioTracks` | Merge exactly two audio streams, preserve other streams, validate, then replace |
| `MKV_FIX.REG` | Compress to MP4 / Compress to AV1 | `HKCU\...\SystemFileAssociations\.mkv` | Re-mux to MP4 or re-encode to AV1 through the shared media runner |
| `NEW_PROJ.REG` | NEW PROJ | `HKCR\Directory\Background\shell\CreateNewProjectFolder` | Create `_new_project` with `ae`, `c4d`, `_input` and `_output` |
| `PACK.REG` | Pack Into Folder | `HKCR\*\shell\PackIntoFolder` | Pack the selected file or folder into a collision-safe adjacent folder |
| `PS_ADMIN.REG` | PS ADMIN | `HKCR\Directory\Background\shell\OpenPowerShellAdmin` | Open PowerShell as administrator in the selected folder |
| `TAKE_OWN.REG` | TAKE OWN | `HKCR\*\shell\TakeOwnership` | Assign ownership of the selected item to the current user |
| `TOGGLE_HID.REG` | Hidden Files | `HKCR\Directory\Background\shell\HiddenFiles` | Toggle hidden-file visibility |

## Encoding and relocation

- `MKV_FIX.REG`, `FFMPEG_MENU.REG`, `DL_YT.REG` and `PACK.REG` are UTF-16 LE
  with BOM because their labels contain non-ASCII text. The remaining active
  files are ANSI/ASCII.
- Path-bearing command values use `%%ROOT%%`; the token is replaced with the
  escaped toolkit root at import time. Never import these files with a raw
  `reg import`.
- `Registry\IMPORT_SAFE.PS1` is the supported single-file installer. It detects
  BOM encoding, substitutes `%%ROOT%%`, imports through the shared mutation
  lock, and reads intended values back because `reg.exe` may exit zero while
  silently skipping an unparseable line.
- `Installers\INSTALL_ALL.PS1` and `INSTALL_GUI.PS1` use the same substitution
  and lock. They resolve all source files, launchers, recorded-generation
  manifests and marker destinations before the first registry mutation.
- The locale marker `Installers\.installed_lang` is JSON containing the applied
  language plus exact source filenames. A marker write failure is a failed
  transaction and rolls the managed registry state back.

## Optional registry menus

These menus are separate from the 14-file default generation:

| Installer | Scope | Behavior |
|---|---|---|
| `Installers\INSTALL_AI_AGENT_MENUS.PS1` | OpenCode / Cline | Per-user `HKCU` menus plus Golden Default console profile; transactional snapshot/rollback; `-VerifyOnly` verifies command, console and Vintage-skill contracts |
| `Add-CodexContextMenus.ps1` | Three isolated Codex accounts | Machine-wide `HKLM` cascade with `SubCommands=""`; transactional and normally elevated |
| `Installers\INSTALL_CONSOLES_MENU.PS1` | All enabled `consoles.json` profiles | Per-user, non-elevated `SaitulsAgents` cascade for folder, background and drive targets; generated in JSON order; transactional snapshot/rollback |

The console installer uses `%V.` for drive roots so the trailing backslash
cannot escape the command's closing quote. `CONSOLES.ps1` removes the marker
suffix before using the path. `INSTALL_ALL.PS1` does not install the console
cascade: run `Add-AgentConsolesMenu.ps1` explicitly.

## Encoding notes and removed files

- `_UTF16` and `_REWRITTEN` artifacts are not installed.
- `MKV_FIX_UTF16.REG` was archived to `_TRASH_20260807/` on 2026-08-27;
  `MKV_FIX.REG` is the canonical UTF-16 LE file.
- `FFMPEG.REG` combined whole-key deletes with 14 unquoted FFmpeg commands.
  It was excluded by T-011 and removed by T-020/T-021. `FFMPEG_MENU.REG`
  provides current install coverage; `FFMPEG_REM.REG` remains for legacy
  removal. Installers still filter the filename defensively.
