"""DEL JUNK rule sets -- the AGGRESSIVE_PROJECT name/pattern tables.

Extracted verbatim from the historical DEL_JUNK.PYW tables so the aggressive
policy and the policy module's self-check share ONE definition. Nothing else
imports this; DEL_JUNK.PYW keeps its own tables for the aggressive path until
the policy module is wired in.
"""

JUNK_EXTENSIONS = {
    # temp / incomplete transfers — always junk
    ".tmp", ".temp", ".part", ".crdownload", ".download", ".opdownload",
    # regenerable build / bytecode artifacts
    ".pyc", ".pyo", ".o", ".obj", ".ilk", ".idb", ".pch", ".sdf",
    ".bsc", ".ncb", ".suo",
    # crash / trace dumps — regenerable, no data value
    ".dmp", ".mdmp", ".minidump", ".crash", ".etl", ".wpr", ".wpa",
    ".blg", ".evtx",
    # editor litter
    ".swp", ".swo", ".swn",
    # OS / explorer litter
    ".ds_store", ".thumbs.db",
    # app cache payload files
    ".cache",
    # logs
    ".log",
}

JUNK_DIR_NAMES = {
    # python / tooling caches — regenerable
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".hypothesis", ".cache",
    # generic cache / temp / log dirs
    "cache", "caches", "temp", "tmp", "logs", "log",
    "crashpad", "crashes", "dumps", "minidumps",
    "updater",
    # browser / app caches (exact compound names only, no substring)
    "gpucache", "shadercache", "grshadercache", "dawncache",
    "graphitedawncache", "thumbnailcache", "webcache", "cache2",
    "startupcache", "code cache", "browsermetrics", "local traces",
    "component_crx_cache", "extensions_crx_cache",
    # node / frontend dependency dirs — regenerable via package manager
    "node_modules", "bower_components",
}

# build output dirs — regenerable (Maven/Rust/etc). Exact name only.
JUNK_BUILD_OUTPUT_DIRS = {
    "target", "build", "dist",
}

JUNK_FILE_PATTERNS = [
    "*.log", "*.tmp", "*.temp", "*.swp", "*.pyc",
    "*.cache", "*.dmp", "*.crash", "*.etl",
    "thumbs.db", ".ds_store",
    "*.exe.tmp", "*.part", "*.crdownload",
    "run-log-*.zip",
]

JUNK_DIR_PATTERNS = [
    "__pycache__", ".pytest_cache", "ruff_cache",
    ".mypy_cache", "node_modules",
]
