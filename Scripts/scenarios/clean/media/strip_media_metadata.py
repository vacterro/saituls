"""
META_WIPE.py  v1.0
----------------------
Universal metadata sanitizer for media files.
Strips EXIF from images and ID3/Metadata from audio using purely native libraries.
Satisfies Socratic Debater Constraint: NO EXTERNAL BINARIES.
"""

import os
import sys
import sqlite3
import logging
import time
import threading
import argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Tuple, Optional

try:
    from PIL import Image
    import mutagen
except ImportError:
    print("[ERROR] Missing required libraries.")
    print("        Run: pip install Pillow mutagen")
    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

ROOT_DIRS: List[str] = []  # A target must be selected explicitly.

# T-166 §13: persistent runtime state (marker DB + log) must NOT land in the
# repository, the selected media folder or a random launcher CWD. It lives
# under %LOCALAPPDATA%\SAITULS\SCENARIOS\strip-media-metadata (overridable
# via SAITULS_SCENARIOS_STATE for tests).
def _state_dir() -> Path:
    base = os.environ.get(
        "SAITULS_SCENARIOS_STATE",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "SAITULS", "SCENARIOS",
                     "strip-media-metadata"),
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p

_STATE = _state_dir()
MARKER_DB = str(_STATE / "META_WIPE.db")
LOG_FILE = str(_STATE / "META_WIPE.log")
DEFAULT_WORKERS = os.cpu_count() or 4

SUPPORTED_EXT = {
    ".jpg":  "image",
    ".jpeg": "image",
    ".png":  "image",
    ".mp3":  "audio",
}

# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

_db_lock = threading.Lock()
_db_conn: Optional[sqlite3.Connection] = None

def _get_db(db_path: str) -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(db_path, check_same_thread=False, timeout=60.0)
        _db_conn.execute("PRAGMA journal_mode=WAL;")
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS processed (
                path        TEXT    NOT NULL,
                mtime_ns    INTEGER NOT NULL,
                size_before INTEGER NOT NULL,
                size_after  INTEGER NOT NULL,
                done_at     INTEGER NOT NULL,
                PRIMARY KEY (path, mtime_ns, size_before)
            )
        """)
        _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_path ON processed (path)")
        _db_conn.commit()
    return _db_conn

def db_is_done(db_path: str, path: Path, mtime_ns: int, size: int) -> bool:
    with _db_lock:
        db = _get_db(db_path)
        row = db.execute(
            "SELECT 1 FROM processed WHERE path=? AND mtime_ns=? AND size_before=?",
            (str(path), mtime_ns, size),
        ).fetchone()
    return row is not None

def db_mark_done(db_path: str, path: Path, mtime_ns: int, size_before: int, size_after: int) -> None:
    with _db_lock:
        db = _get_db(db_path)
        db.execute(
            """
            INSERT OR REPLACE INTO processed (path, mtime_ns, size_before, size_after, done_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(path), mtime_ns, size_before, size_after, int(time.time())),
        )
        db.commit()

@dataclass
class Result:
    path: Path
    ok: bool
    skipped: bool = False
    size_before: int = 0
    size_after: int = 0
    error: str = ""
    mtime_ns: int = 0

def wipe_image(path: Path) -> Tuple[bool, int, str]:
    """Strip EXIF from image by extracting raw data and saving without info."""
    try:
        with Image.open(path) as img:
            data = list(img.getdata())
            img_without_exif = Image.new(img.mode, img.size)
            img_without_exif.putdata(data)
            # Save preserving format, stripping all info dict/exif
            img_without_exif.save(path, format=img.format)
        return True, path.stat().st_size, ""
    except Exception as e:
        return False, 0, str(e)

def wipe_audio(path: Path) -> Tuple[bool, int, str]:
    """Strip ID3/metadata from audio using mutagen delete()."""
    try:
        f = mutagen.File(path)
        if f is not None:
            f.delete()
            f.save()
        return True, path.stat().st_size, ""
    except Exception as e:
        return False, 0, str(e)

def _worker(args: Tuple) -> Result:
    path_str, fmt, db_path = args
    p = Path(path_str)
    stat = p.stat()
    size_before = stat.st_size
    mtime_ns = stat.st_mtime_ns

    if db_is_done(db_path, p, mtime_ns, size_before):
        return Result(path=p, ok=True, skipped=True, size_before=size_before, size_after=size_before, mtime_ns=mtime_ns)

    r = Result(path=p, ok=False, size_before=size_before, mtime_ns=mtime_ns)

    if fmt == "image":
        ok, size_after, err = wipe_image(p)
    else:
        ok, size_after, err = wipe_audio(p)

    r.ok = ok
    r.size_after = size_after if ok else size_before
    r.error = err

    if ok:
        db_mark_done(db_path, p, mtime_ns, size_before, size_after)

    return r

def collect_files(root: Path) -> List[Tuple[Path, str]]:
    files = []
    for entry in root.rglob("*"):
        if entry.is_file():
            ext = entry.suffix.lower()
            fmt = SUPPORTED_EXT.get(ext)
            if fmt:
                files.append((entry, fmt))
    return files

def main():
    parser = argparse.ArgumentParser(description="Metadata Wiper")
    parser.add_argument("target", nargs="?", default=None,
                        help="Target folder (positional; SCENARIOS launcher passes the chosen folder here)")
    parser.add_argument("--dir", default=None, help="Legacy override for the target folder")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args()

    # Precedence: explicit positional target > explicit --dir > legacy defaults.
    if args.target:
        target = Path(args.target)
        if not target.is_dir():
            log.error(f"[REFUSE] target is not an existing folder: {target}")
            sys.exit(2)
        dirs = [target]
    elif args.dir:
        target = Path(args.dir)
        if not target.is_dir():
            log.error(f"[REFUSE] --dir is not an existing folder: {target}")
            sys.exit(2)
        dirs = [target]
    else:
        parser.error("Choose a target folder with --dir before running this tool")

    db_path = MARKER_DB
    _get_db(db_path)

    log.info(f"Workers   : {args.workers}")
    log.info(f"Marker DB : {db_path}")

    grand_ok = 0
    for root in dirs:
        if not root.exists():
            continue
        log.info(f"Scanning: {root}")
        files = collect_files(root)
        total = len(files)
        
        if total == 0:
            continue

        tasks = [(str(p), fmt, db_path) for p, fmt in files]
        
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_worker, t): t for t in tasks}
            for fut in as_completed(futures):
                r = fut.result()
                if r.ok and not r.skipped:
                    grand_ok += 1
                    log.info(f"  OK   {r.path.name}")
                elif not r.ok:
                    log.warning(f"  FAIL {r.path.name} - {r.error}")

    log.info(f"Processed: {grand_ok} files newly wiped.")

if __name__ == "__main__":
    main()
