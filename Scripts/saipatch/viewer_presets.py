"""
viewer_presets.py - Audit preset management for Queue Viewer.
Manages persistent preset mappings in %LOCALAPPDATA%\\SAITULS\\SAIPATCH\\queue-viewer-presets.json.

PURE STDLIB module (no Qt) so it can run in any worker thread. All preset
file I/O (load/save/read) executes in the PresetWorker QThread worker, never
on the Qt GUI thread: preset files may live on slow, offline or removable
paths. Retained limits: 1 MiB max, UTF-8 required, binary/NUL rejection.
File contents are never logged.
"""
import os
import json
from typing import Optional, Dict

PRESETS_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "SAITULS", "SAIPATCH")
PRESETS_FILE = os.path.join(PRESETS_DIR, "queue-viewer-presets.json")
MAX_FILE_SIZE = 1 * 1024 * 1024  # 1 MiB
SLOTS = ("core", "wave2", "performance")


def load_presets() -> Dict[str, str]:
    """Load preset file paths from persistent storage."""
    if not os.path.isfile(PRESETS_FILE):
        return {}
    try:
        with open(PRESETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if k in SLOTS and isinstance(v, str)}
    except Exception:
        return {}


def save_preset(slot: str, path: str) -> None:
    """Save a preset file path. Creates directory if needed."""
    if slot not in SLOTS:
        raise ValueError(f"Invalid slot: {slot}")
    data = load_presets()
    data[slot] = path
    os.makedirs(PRESETS_DIR, exist_ok=True)
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def read_preset_text(slot: str) -> Optional[str]:
    """Read the text content of a preset file.
    Returns None if not configured or file missing.
    Raises ValueError on oversized or binary file.
    """
    data = load_presets()
    path = data.get(slot)
    if not path or not os.path.isfile(path):
        return None
    size = os.path.getsize(path)
    if size > MAX_FILE_SIZE:
        raise ValueError(f"Preset file too large: {size} bytes (max {MAX_FILE_SIZE})")
    with open(path, "rb") as f:
        raw = f.read(MAX_FILE_SIZE + 1)
    if b"\x00" in raw:
        raise ValueError("Binary file detected, preset must be text")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("File is not valid UTF-8 text")


def get_preset_path(slot: str) -> Optional[str]:
    """Get the configured file path for a preset slot.

    NOTE: performs file I/O (PRESETS_FILE stat+read). Call only from a
    worker thread. The GUI path uses PresetWorker cache/state instead.
    """
    return load_presets().get(slot)
