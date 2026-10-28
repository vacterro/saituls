# ============================================================
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
#  TGA2DDS2.py
#
#  MODE 1 (TGA found):
#    TGA -> DDS
#
#  MODE 2 (DDS found):
#    DDS -> TGA
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


# ------------------------------------------------------------
def find_texconv():
    for p in _texconv_candidates():
        p = Path(str(p))
        if p.exists():
            return p
    return None


# ------------------------------------------------------------
def run_texconv(src, dst, texconv, fmt=None):
    tmp_dir = SCRIPT_DIR / "_texconv_tmp"
    tmp_dir.mkdir(exist_ok=True)

    cmd = [
        str(texconv),
        "-ft",
        dst.suffix.lstrip("."),
        "-y",
        "-o",
        str(tmp_dir),
    ]

    if fmt:
        cmd.extend(["-f", fmt])

    cmd.append(str(src))

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120
        )

        tmp_out = tmp_dir / (src.stem + dst.suffix)

        if tmp_out.exists():
            shutil.move(str(tmp_out), str(dst))
            return True

        err = result.stderr.decode(errors="replace").strip()
        out = result.stdout.decode(errors="replace").strip()

        print(f"  [FAIL] texconv: {err or out or 'no output'}")
        return False

    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


# ------------------------------------------------------------
def process(files, texconv, dst_ext, fmt=None):
    ok = 0
    skip = 0
    fail = 0

    for src in files:

        dst = src.with_suffix(dst_ext)

        print(f"-> {src.name}")

        if SKIP_EXISTING and dst.exists():
            print(f"  [SKIP] {dst.name}")
            skip += 1
            print()
            continue

        if run_texconv(src, dst, texconv, fmt):
            print(f"  [OK]   {dst.name}")
            ok += 1
        else:
            fail += 1

        print()

    return ok, skip, fail


# ------------------------------------------------------------
def main():

    texconv = find_texconv()

    if not texconv:
        print("[FAIL] texconv.exe not found")
        print("       Get it: https://github.com/microsoft/DirectXTex/releases")
        print("       Put it next to this script, in C:\\tools, on PATH, or")
        print("       list it in %LOCALAPPDATA%\\SAITULS\\tool-paths.json")
        raise SystemExit(2)

    print(f"[OK] texconv: {texconv}\n")

    tga_files = sorted(SCRIPT_DIR.glob("*.tga"))
    dds_files = sorted(SCRIPT_DIR.glob("*.dds"))

    if tga_files:

        print(f"[MODE] TGA -> DDS ({DDS_FORMAT})")
        print(f"[INFO] Found {len(tga_files)} file(s)\n")

        ok, skip, fail = process(
            tga_files,
            texconv,
            ".dds",
            DDS_FORMAT
        )

    elif dds_files:

        print("[MODE] DDS -> TGA")
        print(f"[INFO] Found {len(dds_files)} file(s)\n")

        ok, skip, fail = process(
            dds_files,
            texconv,
            ".tga"
        )

    else:

        print(f"[INFO] No TGA or DDS found in:\n{SCRIPT_DIR}")
        return

    tmp = SCRIPT_DIR / "_texconv_tmp"

    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 50)
    print(f"Done. OK={ok} Skipped={skip} Failed={fail}")
    print("=" * 50)


if __name__ == "__main__":
    main()