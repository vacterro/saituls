"""
COMPRESS.py  v2.0
----------------------
Lossless batch compression for large reference photo collections.
Uses: jpegoptim (JPG) + oxipng (PNG) via subprocess.
Requires: jpegoptim.exe and oxipng.exe in PATH or TOOLS_DIR below.

Install tools (choose one):
  winget install --id=jpegoptim.jpegoptim
  winget install --id=shssoichiro.oxipng

  OR Chocolatey:
  choco install jpegoptim oxipng

Features v2.0:
  - Multiple ROOT_DIRS, processed strictly top-to-bottom
  - SQLite marker database: tracks processed files by path+mtime+size,
    never renames or touches image files themselves
  - Strict lossless: alpha channels, ICC profiles, EXIF, timestamps preserved
  - Skip-on-rerun: already-compressed files are detected and skipped instantly

Usage:
  python COMPRESS.py
  python COMPRESS.py --dry-run            # stats only, no file changes
  python COMPRESS.py --workers 8          # parallel workers (default: cpu_count)
  python COMPRESS.py --level 6            # oxipng level 0-6 (default: 4)
  python COMPRESS.py --reset-db           # clear marker DB, reprocess everything
  python COMPRESS.py --dir "D:\\Photos"   # override dirs (single dir, CLI only)
"""

import os
import sys
import sqlite3
import subprocess
import argparse
import logging
import shutil
import time
import threading
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Tuple, Optional

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG  —  edit here
# ─────────────────────────────────────────────────────────────────────────────

# Directories are processed strictly in list order, top to bottom.
ROOT_DIRS: List[str] = []  # A target must be selected explicitly.

# Folder with jpegoptim.exe / oxipng.exe — defaults to script directory
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))

# SQLite database file that stores the "already processed" markers.
# One shared DB covers all ROOT_DIRS.  Safe to delete to reprocess everything.
MARKER_DB = r"COMPRESS.db"

# Log file
LOG_FILE = r"COMPRESS.log"

# Backup originals to <file>.bak before overwriting?  Safe but slow.
BACKUP_BEFORE = False

DEFAULT_WORKERS = os.cpu_count() or 4

# oxipng optimisation level: 0 (fastest) .. 6 (smallest / slowest).
# 4 is the recommended balance.  Level does NOT affect quality or alpha.
OXIPNG_LEVEL = 4

# jpegoptim: extra flags.  Do NOT add --strip-all here — it removes ICC / EXIF.
# jpegoptim always runs lossless (no --max flag).
JPEGOPTIM_EXTRA_FLAGS: List[str] = []

SUPPORTED_EXT = {
    ".jpg":  "jpeg",
    ".jpeg": "jpeg",
    ".png":  "png",
}

# ─────────────────────────────────────────────────────────────────────────────


# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Marker database ───────────────────────────────────────────────────────────
# Uses a thread-local connection so the DB can be safely used from the main
# process while workers report results back.
#
# Schema: path TEXT + mtime_ns INTEGER + size_before INTEGER form the primary
# key.  If a file matches all three it was already successfully compressed —
# even if someone edited+restored a file the mtime_ns or size will differ,
# so it will be re-processed correctly.

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
    """Return True if this exact file state was already successfully processed."""
    with _db_lock:
        db = _get_db(db_path)
        row = db.execute(
            "SELECT 1 FROM processed WHERE path=? AND mtime_ns=? AND size_before=?",
            (str(path), mtime_ns, size),
        ).fetchone()
    return row is not None


def db_mark_done(db_path: str, path: Path, mtime_ns: int, size_before: int, size_after: int) -> None:
    """Record a successfully compressed file."""
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


def db_reset(db_path: str) -> None:
    with _db_lock:
        db = _get_db(db_path)
        db.execute("DELETE FROM processed")
        db.commit()
    log.warning("Marker database cleared — all files will be reprocessed.")


# ── Tool discovery ─────────────────────────────────────────────────────────────
def find_tool(name: str) -> str:
    if TOOLS_DIR:
        candidate = Path(TOOLS_DIR) / (name + ".exe")
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"Tool '{name}' not found.  Add to PATH or set TOOLS_DIR.\n"
        f"  jpegoptim: https://github.com/tjko/jpegoptim/releases\n"
        f"  oxipng   : https://github.com/shssoichiro/oxipng/releases"
    )


# ── Result dataclass ──────────────────────────────────────────────────────────
@dataclass
class Result:
    path: Path
    ok: bool
    skipped: bool = False       # already in DB
    size_before: int = 0
    size_after: int = 0
    error: str = ""
    mtime_ns: int = 0

    @property
    def saved(self) -> int:
        return max(0, self.size_before - self.size_after)

    @property
    def ratio(self) -> float:
        if self.size_before == 0:
            return 0.0
        return self.saved / self.size_before * 100


# ── Compression workers ────────────────────────────────────────────────────────
def compress_jpeg(
    path: Path,
    jpegoptim_bin: str,
    dry_run: bool,
    backup: bool,
) -> Tuple[bool, int, str]:
    """
    Run jpegoptim losslessly.
    Flags used:
      --preserve       : keep original file timestamps
      (NO --strip-all) : keep EXIF, ICC profile, comments — full metadata intact
    Returns (ok, size_after, error_msg).
    """
    if dry_run:
        # --noaction: analyse only, never writes
        cmd = [jpegoptim_bin, "--noaction", str(path)]
    else:
        if backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        cmd = [jpegoptim_bin, "--preserve", "--totals"] + JPEGOPTIM_EXTRA_FLAGS + [str(path)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return False, 0, (proc.stderr or proc.stdout).strip()
        size_after = path.stat().st_size if not dry_run else path.stat().st_size
        return True, size_after, ""
    except Exception as e:
        return False, 0, str(e)


def compress_png(
    path: Path,
    oxipng_bin: str,
    level: int,
    dry_run: bool,
    backup: bool,
) -> Tuple[bool, int, str]:
    """
    Run oxipng losslessly.
    Flags used:
      -o<level>        : optimisation level (0-6), never affects pixel data
      --preserve       : keep original timestamps
      (NO --strip)     : keep all ancillary chunks — alpha (tRNS), ICC (iCCP),
                         text metadata, etc.  Alpha as RGBA is always preserved
                         because oxipng only rewrites IDAT (deflate stream).
    Returns (ok, size_after, error_msg).
    """
    if dry_run:
        # --dry-run: compute savings without writing
        cmd = [oxipng_bin, "--dry-run", f"-o{level}", str(path)]
    else:
        if backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        cmd = [oxipng_bin, f"-o{level}", "--preserve", str(path)]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            return False, 0, (proc.stderr or proc.stdout).strip()
        size_after = path.stat().st_size
        return True, size_after, ""
    except Exception as e:
        return False, 0, str(e)


# ── Worker entry point (top-level, picklable for ProcessPoolExecutor) ─────────
def _worker(args: Tuple) -> Result:
    (
        path_str, fmt,
        jpegoptim_bin, oxipng_bin,
        dry_run, backup, level,
        db_path,
    ) = args

    p = Path(path_str)
    stat = p.stat()
    size_before = stat.st_size
    mtime_ns    = stat.st_mtime_ns

    # Fast skip: already processed with identical state
    if db_is_done(db_path, p, mtime_ns, size_before):
        return Result(path=p, ok=True, skipped=True,
                      size_before=size_before, size_after=size_before,
                      mtime_ns=mtime_ns)

    r = Result(path=p, ok=False, size_before=size_before, mtime_ns=mtime_ns)

    if fmt == "jpeg":
        ok, size_after, err = compress_jpeg(p, jpegoptim_bin, dry_run, backup)
    else:
        ok, size_after, err = compress_png(p, oxipng_bin, level, dry_run, backup)

    r.ok         = ok
    r.size_after = size_after if ok else size_before
    r.error      = err

    # Write marker only on real success (not dry-run, not error)
    if ok and not dry_run:
        db_mark_done(db_path, p, mtime_ns, size_before, size_after)

    return r


# ── File collection ────────────────────────────────────────────────────────────
def collect_files(root: Path) -> List[Tuple[Path, str]]:
    files = []
    for entry in root.rglob("*"):
        if entry.is_file():
            ext = entry.suffix.lower()
            fmt = SUPPORTED_EXT.get(ext)
            if fmt:
                files.append((entry, fmt))
    return files


# ── Helpers ────────────────────────────────────────────────────────────────────
def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Lossless batch image compressor v2.0")
    parser.add_argument("--dir",      default=None,              help="Override ROOT_DIRS with a single directory")
    parser.add_argument("--workers",  type=int, default=DEFAULT_WORKERS, help="Parallel workers")
    parser.add_argument("--dry-run",  action="store_true",       help="Preview only, no file changes")
    parser.add_argument("--backup",   action="store_true",       help="Save .bak before overwriting")
    parser.add_argument("--level",    type=int, default=OXIPNG_LEVEL, help="oxipng level 0-6")
    parser.add_argument("--reset-db", action="store_true",       help="Clear marker DB and reprocess everything")
    parser.add_argument("--db",       default=MARKER_DB,         help="Path to SQLite marker database")
    args = parser.parse_args()

    db_path = args.db

    # Resolve directory list
    if args.dir:
        dirs = [Path(args.dir)]
    else:
        parser.error("Choose a target folder with --dir before running this tool")

    for d in dirs:
        if not d.exists():
            log.error(f"Directory not found: {d}")
            sys.exit(1)

    # Optional DB reset
    if args.reset_db:
        db_reset(db_path)

    # Ensure DB is initialised before spawning workers
    _get_db(db_path)

    # Find tools
    try:
        jpegoptim_bin = find_tool("jpegoptim")
        oxipng_bin    = find_tool("oxipng")
        log.info(f"jpegoptim : {jpegoptim_bin}")
        log.info(f"oxipng    : {oxipng_bin}")
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    mode = "DRY RUN (no files written)" if args.dry_run else "LIVE MODE"
    log.info(f"Mode      : {mode}")
    log.info(f"Workers   : {args.workers}")
    log.info(f"Marker DB : {db_path}")
    log.info(f"Dirs      : {len(dirs)}")
    for i, d in enumerate(dirs, 1):
        log.info(f"  [{i}] {d}")
    log.info("-" * 70)

    # ── Accumulate stats across all directories ────────────────────────────────
    grand_ok       = 0
    grand_skipped  = 0
    grand_improved = 0
    grand_fail     = 0
    grand_before   = 0
    grand_after    = 0
    t_grand_start  = time.time()

    for dir_idx, root in enumerate(dirs, 1):
        log.info(f"[{dir_idx}/{len(dirs)}] Scanning: {root}")
        files = collect_files(root)
        total = len(files)
        log.info(f"           Found: {total} image files")

        if total == 0:
            log.info("           Nothing to compress here, moving on.")
            continue

        tasks = [
            (str(p), fmt,
             jpegoptim_bin, oxipng_bin,
             args.dry_run, args.backup, args.level,
             db_path)
            for p, fmt in files
        ]

        results: List[Result] = []
        done = 0
        t_dir_start = time.time()

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(_worker, t): t for t in tasks}
            for fut in as_completed(futures):
                done += 1
                r: Result = fut.result()
                results.append(r)

                elapsed = time.time() - t_dir_start
                fps     = done / elapsed if elapsed > 0 else 0
                eta     = (total - done) / fps if fps > 0 else 0

                if r.skipped:
                    log.debug(f"  SKIP [{done}/{total}] {r.path.name} (already in DB)")
                elif not r.ok:
                    log.warning(f"  FAIL [{done}/{total}] {r.path.name} — {r.error}")
                elif r.saved > 0:
                    log.info(
                        f"  OK   [{done}/{total}] {r.path.name:50s} "
                        f"{human_size(r.size_before):>10} -> {human_size(r.size_after):>10} "
                        f"(-{r.ratio:.1f}%)"
                    )
                else:
                    log.debug(f"  SKIP [{done}/{total}] {r.path.name} (already optimal)")

                print(
                    f"\r  Dir [{dir_idx}/{len(dirs)}]  {done}/{total}  |  "
                    f"{fps:.1f} f/s  |  ETA {int(eta // 60)}m {int(eta % 60)}s    ",
                    end="", flush=True,
                )

        print()  # newline after progress bar

        # Per-directory stats
        ok_list      = [r for r in results if r.ok and not r.skipped]
        skip_list    = [r for r in results if r.skipped]
        fail_list    = [r for r in results if not r.ok]
        improved     = [r for r in ok_list if r.saved > 0]
        dir_before   = sum(r.size_before for r in ok_list)
        dir_after    = sum(r.size_after  for r in ok_list)
        dir_saved    = dir_before - dir_after
        dir_elapsed  = time.time() - t_dir_start

        log.info(f"  Dir summary  [{root.name}]")
        log.info(f"    Processed  : {len(ok_list)}")
        log.info(f"    Skipped    : {len(skip_list)}  (already in marker DB)")
        log.info(f"    Improved   : {len(improved)}")
        log.info(f"    Errors     : {len(fail_list)}")
        if dir_before > 0:
            log.info(f"    Saved      : {human_size(dir_saved)} ({dir_saved/dir_before*100:.2f}%)")
        log.info(f"    Time       : {dir_elapsed/60:.1f} min")
        log.info("-" * 70)

        if fail_list:
            log.warning(f"  Failed files in {root}:")
            for r in fail_list:
                log.warning(f"    {r.path}  —  {r.error}")

        grand_ok       += len(ok_list)
        grand_skipped  += len(skip_list)
        grand_improved += len(improved)
        grand_fail     += len(fail_list)
        grand_before   += dir_before
        grand_after    += dir_after

    # ── Grand summary ──────────────────────────────────────────────────────────
    grand_elapsed = time.time() - t_grand_start
    grand_saved   = grand_before - grand_after

    log.info("=" * 70)
    log.info("  GRAND TOTAL")
    log.info(f"  Dirs processed  : {len(dirs)}")
    log.info(f"  Files processed : {grand_ok}")
    log.info(f"  Files skipped   : {grand_skipped}  (marker DB hit)")
    log.info(f"  Files improved  : {grand_improved}")
    log.info(f"  Errors          : {grand_fail}")
    log.info(f"  Size before     : {human_size(grand_before)}")
    log.info(f"  Size after      : {human_size(grand_after)}")
    if grand_before > 0:
        log.info(f"  Total saved     : {human_size(grand_saved)} ({grand_saved/grand_before*100:.2f}%)")
    else:
        log.info("  Total saved     : 0 B")
    log.info(f"  Total time      : {grand_elapsed/60:.1f} min")
    log.info("=" * 70)
    log.info(f"Marker DB : {db_path}")
    log.info(f"Log file  : {LOG_FILE}")


if __name__ == "__main__":
    main()
