# ============================================================
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
#  TGA2DDS.py
#
#  TGA -> DDS
#
#  GET texconv.exe:
#    https://github.com/microsoft/DirectXTex/releases
# ============================================================

import os
import subprocess
import shutil
import sys
from pathlib import Path


def resolve_target():
    """T-166 launch contract: the launcher's positional target is the authority."""
    if len(sys.argv) < 2:
        return None
    t = Path(sys.argv[1]).expanduser().resolve()
    if not t.is_dir():
        print(f"[REFUSE] target is not an existing folder: {t}", file=sys.stderr)
        raise SystemExit(2)
    return t


SCRIPT_DIR = resolve_target() or Path(__file__).resolve().parent

SKIP_EXISTING = False
DDS_FORMAT = "BC7_UNORM"

TEXCONV_CANDIDATES = [
    SCRIPT_DIR / "texconv.exe",
    Path(r"C:\tools\texconv.exe"),
]


def _texconv_candidates():
    cands = list(TEXCONV_CANDIDATES)
    cfg = Path(os.environ.get("LOCALAPPDATA", "")) / "SAITULS" / "tool-paths.json"
    if cfg.is_file():
        try:
            import json
            for entry in json.loads(cfg.read_text(encoding="utf-8")).get("texconv", []):
                cands.append(Path(entry))
        except Exception:
            pass
    w = shutil.which("texconv")
    if w:
        cands.append(Path(w))
    return cands


def find_texconv():
    for p in _texconv_candidates():
        p = Path(str(p))
        if p.exists():
            return p
    return None


def convert(src, dst, texconv):

    tmp_dir = SCRIPT_DIR / "_texconv_tmp"
    tmp_dir.mkdir(exist_ok=True)

    cmd = [
        str(texconv),
        "-ft", "dds",
        "-f", DDS_FORMAT,
        "-y",
        "-o", str(tmp_dir),
        str(src)
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120
        )

        tmp_out = tmp_dir / (src.stem + ".dds")

        if tmp_out.exists():
            shutil.move(str(tmp_out), str(dst))
            return True

        print(result.stderr.decode(errors="replace"))
        return False

    except Exception as e:
        print(e)
        return False


def main():

    texconv = find_texconv()

    if not texconv:
        print("[FAIL] texconv.exe not found")
        print("       Get it: https://github.com/microsoft/DirectXTex/releases")
        print("       Put it next to this script, in C:\\tools, on PATH, or")
        print("       list it in %LOCALAPPDATA%\\SAITULS\\tool-paths.json")
        raise SystemExit(2)

    files = sorted(SCRIPT_DIR.glob("*.tga"))

    if not files:
        print("[INFO] No TGA files found")
        return

    ok = 0
    fail = 0

    print(f"[INFO] Found {len(files)} TGA file(s)\n")

    for src in files:

        dst = src.with_suffix(".dds")

        print(f"-> {src.name}")

        if SKIP_EXISTING and dst.exists():
            print("  [SKIP]")
            continue

        if convert(src, dst, texconv):
            print(f"  [OK] {dst.name}")
            ok += 1
        else:
            print("  [FAIL]")
            fail += 1

        print()

    shutil.rmtree(
        SCRIPT_DIR / "_texconv_tmp",
        ignore_errors=True
    )

    print("=" * 50)
    print(f"Done. OK={ok} Failed={fail}")
    print("=" * 50)


if __name__ == "__main__":
    main()