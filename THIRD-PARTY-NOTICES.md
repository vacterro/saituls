# Third-party notices

SAITULS itself is MIT (see `LICENSE`). The release payload
(`SAITULS-payload-<version>.zip`) redistributes the tools below. Each stays
under its own licence, which travels with it here.

## FFmpeg and ffprobe

- **Project:** FFmpeg — https://ffmpeg.org/
- **Build:** `2025-08-14-git-cdbb5f1b93-full_build`, from
  https://www.gyan.dev/ffmpeg/builds/
- **Files:** `Bin/FFMPEG.EXE`, `Bin/FFPROBE.EXE`, `Bin/App/ffmpeg.exe`,
  `Bin/App/FFPROBE.EXE`, `Bin/App/AV1 CPU/FFMPEG.EXE`
- **Licence:** GNU GPL v3 or later. This build is compiled with
  `--enable-gpl --enable-version3`, so the GPLv3 terms apply; the licence text
  and the complete corresponding source are available from the project at the
  URL above. Redistributing the unmodified build keeps it under those terms.

## yt-dlp

- **Project:** yt-dlp — https://github.com/yt-dlp/yt-dlp
- **Files:** `Bin/yt-dlp.exe`, `Bin/App/yt-dlp.exe`
- **Licence:** The Unlicense (public domain dedication). The project's
  `LICENSE` file states the terms.

## aria2

- **Project:** aria2 — https://github.com/aria2/aria2
- **Files:** `Bin/ARIA2C.EXE`, `Bin/App/ARIA2C.EXE`
- **Licence:** GNU GPL v2 or later.

## Ghostscript

- **Project:** Ghostscript — https://www.ghostscript.com/
- **Files:** `Bin/App/GS/`
- **Licence:** GNU AGPL v3 for the community release, or a commercial
  Artifex licence. Redistribution here is the unmodified community build; the
  AGPL terms and the corresponding source are available from the project.

## ExifCleaner

- **Project:** ExifCleaner — https://github.com/szTheory/exifcleaner
- **Files:** `Bin/App/ExifCleaner/`
- **Licence:** MIT.

## AV1 and RTX40 encoder bundles

- **Files:** `Bin/App/AV1 CPU/`, `Bin/App/RTX40__/`
- **Contents:** FFmpeg builds configured for AV1 encoding. The same GPLv3
  terms as FFmpeg above apply.

## Wrapper executables

`Bin/LAUNCHER.EXE`, `Bin/__CONTEXTMENU+.EXE`, `Bin/App/AUDIO.EXE` and
`Bin/App/VIDEO.EXE` are the author's own launcher scripts, compiled with
AutoHotkey v1 (GPLv2). They contain no third-party code beyond the AutoHotkey
runtime. AutoHotkey is available at https://www.autohotkey.com/.

## Fonts and icons

`SAITULS.ico` and `heh.ico` are the author's own artwork.

---

The payload is assembled by `BUILD_PAYLOAD.cmd` from unmodified upstream
releases. Nothing here alters a third-party binary; if any of them is updated,
replace the file and update the version line above.
