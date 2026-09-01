# Registry catalog

All context-menu entries live under `Registry/`. The install set is the
14 files below (excluding `*_REM*`, `*_UTF16*`, `*_REWRITTEN*, and
`FFMPEG.REG`). Each has a matching `*_REM.REG` uninstall file.

| File | Menu label | Hive / key | Function |
|---|---|---|---|
| `COPY_PATH.REG` | COPY PATH | `HKCR\*\shell\CopyPathAsText` | Copy full path of the file as text |
| `DEL_DUP.REG` | DEL DUP | `HKCR\Directory\shell\RemoveDuplicateFiles` | Delete duplicate files (SHA-256) |
| `DEL_EMPTY.REG` | DEL EMPTY | `HKCR\Directory\shell\DeleteEmptyFolders` | Delete empty folders |
| `DEL_JUNK.REG` | DEL JUNK | `HKCR\Directory\shell\DeleteJunkFiles` | Delete junk files via SMART_VAC_CLEANER |
| `DEL_SAME.REG` | DEL SAME | `HKCR\Directory\shell\RemoveNestedDuplicates` | Delete nested same-name files |
| `DL_YT.REG` | (Download From Youtube) | `HKLM\SOFTWARE\Classes\Directory\background\shell\DownloadFromYoutube` | 6 yt-dlp modes: audio/video, with/without date, playlist |
| `FFMPEG_MENU.REG` | (convert submenu, Cyrillic) | `HKCU\SOFTWARE\Classes\SystemFileAssociations` | ffmpeg conversion submenu, 23 extensions, 599 keys |
| `MERGE_AUD.REG` | MERGE AUD | `HKCR\*\shell\MergeAudioTracks` | Merge 2 audio tracks into one file |
| `MKV_FIX.REG` | Compress to MP4 / Compress to AV1 (Cyrillic labels) | `HKCU\...\SystemFileAssociations\.mkv` | Re-mux to MP4 or re-encode to AV1 |
| `NEW_PROJ.REG` | NEW PROJ | `HKCR\Directory\Background\shell\CreateNewProjectFolder` | Create `_new_project` skeleton |
| `PACK.REG` | (Pack Into Folder) | `HKCR\*\shell\PackIntoFolder` | Pack file/folder into `*_Packed` |
| `PS_ADMIN.REG` | PS ADMIN | `HKCR\Directory\Background\shell\OpenPowerShellAdmin` | Open PowerShell as administrator |
| `TAKE_OWN.REG` | TAKE OWN | `HKCR\*\shell\TakeOwnership` | Take ownership of the file |
| `TOGGLE_HID.REG` | (Hidden Files) | `HKCR\Directory\Background\shell\HiddenFiles` | Toggle hidden files visibility |

## Encoding notes

- Cyrillic labels require UTF-16 LE with BOM (`MKV_FIX.REG`,
  `FFMPEG_MENU.REG`) — `reg.exe import` garbles UTF-8 Cyrillic.
- `_UTF16` / `_REWRITTEN` files are **not** part of the install set.
- `MKV_FIX_UTF16.REG` (double-encoded mojibake, 0 refs) was archived to
  `_TRASH_20260807/` on 2026-08-27 (T-044); `MKV_FIX.REG` is the canonical
  UTF-16 LE file.

## Removed: `FFMPEG.REG`

Legacy hybrid: opened with whole-key deletes
(`[-HKEY...\SystemFileAssociations\.avi/.flac/.mkv/.mov/.mp4/.wav/.weba]`)
and then installed 14 unquoted `FFMPEG.EXE -i` commands. Excluded from
installers since T-011 and **permanently removed** on 2026-08-07
(T-020/T-021); coverage is fully replaced by `FFMPEG_MENU.REG` (23
extensions) and removal is covered by `FFMPEG_REM.REG`. Installers still
filter the name defensively.
