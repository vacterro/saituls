# ══════════════════════════════════════════════════════════════════════════════
#  MP3 Renamer: renames files by TIT2 (Title) ID3 tag
#  Requirements: pip install mutagen
#  Usage: place this script next to .mp3 files and run it
# ══════════════════════════════════════════════════════════════════════════════

import os
import re
import sys
from pathlib import Path

try:
    from mutagen.id3 import ID3, ID3NoHeaderError
except ImportError:
    print("ERROR: mutagen not installed.")
    print("Run: pip install mutagen")
    sys.exit(1)


def resolve_target():
    """T-166 launch contract: the launcher's positional target is the authority."""
    if len(sys.argv) < 2:
        return None
    t = Path(sys.argv[1]).expanduser().resolve()
    if not t.is_dir():
        print(f"[REFUSE] target is not an existing folder: {t}", file=sys.stderr)
        sys.exit(2)
    return t


def pause_if_interactive():
    # Launched by SCENARIOS with an explicit target -> noninteractive.
    if len(sys.argv) >= 2:
        return
    try:
        input("Press Enter to exit...")
    except EOFError:
        pass


def rename_by_title(full_paths: list) -> list:
    """
    For each MP3: if TIT2 tag != filename -> rename the file.
    Returns updated list of paths.
    Duplicates resolved by appending (1), (2)...
    """
    updated = []

    for filepath in full_paths:
        if not os.path.exists(filepath):
            print(f"  [SKIP] Not found: {filepath}")
            updated.append(filepath)
            continue

        # --- Read ID3 tag ---
        title = ''
        try:
            tags = ID3(filepath)
            if 'TIT2' in tags:
                title = str(tags['TIT2']).strip()
        except ID3NoHeaderError:
            print(f"  [SKIP] No ID3 header: {os.path.basename(filepath)}")
        except Exception as e:
            print(f"  [SKIP] Tag read error ({os.path.basename(filepath)}): {e}")

        if not title:
            print(f"  [SKIP] Empty title tag: {os.path.basename(filepath)}")
            updated.append(filepath)
            continue

        # --- Sanitize title for Windows filename ---
        clean_title = re.sub(r'[\\/:*?"<>|]', '', title).strip()
        clean_title = clean_title.strip('.')  # Remove leading/trailing dots

        if not clean_title:
            print(f"  [SKIP] Title became empty after cleanup: {os.path.basename(filepath)}")
            updated.append(filepath)
            continue

        folder       = os.path.dirname(filepath)
        new_name     = f"{clean_title}.mp3"
        new_path     = os.path.join(folder, new_name)
        current_name = os.path.basename(filepath)

        # Already correct name
        if current_name.lower() == new_name.lower():
            print(f"  [OK]   Already correct: {current_name}")
            updated.append(filepath)
            continue

        # --- Resolve duplicates ---
        counter = 1
        base = clean_title
        while os.path.exists(new_path) and os.path.abspath(new_path) != os.path.abspath(filepath):
            new_name = f"{base} ({counter}).mp3"
            new_path = os.path.join(folder, new_name)
            counter += 1

        # --- Rename ---
        try:
            os.rename(filepath, new_path)
            print(f"  [REN]  {current_name}")
            print(f"      -> {new_name}")
            updated.append(new_path)
        except OSError as e:
            print(f"  [ERR]  Rename failed ({current_name}): {e}")
            updated.append(filepath)

    return updated


def collect_mp3(folder: str) -> list:
    """Collect all .mp3 files in the given folder (non-recursive)."""
    files = [
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith('.mp3') and os.path.isfile(os.path.join(folder, f))
    ]
    files.sort()
    return files


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    folder = resolve_target() or Path(__file__).resolve().parent

    mp3_files = collect_mp3(folder)

    if not mp3_files:
        print(f"No .mp3 files found in: {folder}")
        pause_if_interactive()
        sys.exit(0)

    print(f"Found {len(mp3_files)} MP3 file(s) in: {folder}")
    print("=" * 60)

    rename_by_title(mp3_files)

    print("=" * 60)
    print("Done.")
    pause_if_interactive()