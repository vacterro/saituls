# ============================================================
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
#  DDS2PNG.py  v2
#  Primary:  texconv.exe  (handles ALL DXGI formats incl. BC7,
#            B8G8R8A8_UNORM_SRGB, BC6H, etc.)
#  Fallback: Pillow -> wand
#
#  HOW TO GET texconv.exe:
#    https://github.com/microsoft/DirectXTex/releases
#    Download "texconv.exe" from the latest release assets.
#    Put it next to THIS script  -OR-  anywhere in PATH.
# ============================================================

import os
import subprocess
import shutil
import sys
from pathlib import Path


def resolve_target():
    """T-166 launch contract: the launcher's positional target is the authority.

    Returns the resolved target folder when supplied, else None (legacy
    drop-in behaviour next to the script). An INVALID explicit target must
    refuse loudly: nonzero exit, no mutation, no silent fallback.
    """
    if len(sys.argv) < 2:
        return None
    t = Path(sys.argv[1]).expanduser().resolve()
    if not t.is_dir():
        print(f"[REFUSE] target is not an existing folder: {t}", file=sys.stderr)
        raise SystemExit(2)
    return t


SCRIPT_DIR    = resolve_target() or Path(__file__).resolve().parent
FOLDER_MODE   = len(sys.argv) > 1
INPUT_EXT     = ".dds"
OUTPUT_EXT    = ".png"
SKIP_EXISTING = False

# Where to look for texconv.exe: external tool, discovered through the
# SAITULS shared tool-paths config (LOCALAPPDATA) or PATH. It is NOT bundled.
TEXCONV_CANDIDATES = [
    SCRIPT_DIR / "texconv.exe",          # next to script  <-- easiest
    Path(r"C:\tools\texconv.exe"),        # custom location
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
def convert_via_texconv(src, dst, texconv):
    """
    texconv writes OUTPUT to the SAME folder as INPUT by default.
    We redirect to a temp subfolder, then move the result.
    """
    tmp_dir = SCRIPT_DIR / "_texconv_tmp"
    tmp_dir.mkdir(exist_ok=True)

    cmd = [
        str(texconv),
        "-ft", "png",
        "-y",
        "-o", str(tmp_dir),
        str(src)
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30
        )
        tmp_png = tmp_dir / (src.stem + ".png")

        if tmp_png.exists():
            shutil.move(str(tmp_png), str(dst))
            return True
        else:
            err = result.stderr.decode(errors="replace").strip()
            out = result.stdout.decode(errors="replace").strip()
            print(f"  [FAIL] texconv: {err or out or 'no output png produced'}")
            return False
    except Exception as e:
        print(f"  [FAIL] texconv exception: {e}")
        return False
    finally:
        try:
            tmp_dir.rmdir()
        except Exception:
            pass


# ------------------------------------------------------------
def convert_via_pillow(src, dst):
    try:
        from PIL import Image
        with Image.open(src) as img:
            print(f"  [INFO] Pillow mode={img.mode}, size={img.size}")
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            img.save(dst, format="PNG")
        return True
    except ImportError:
        print("  [SKIP] Pillow not installed: pip install Pillow")
    except Exception as e:
        print(f"  [FAIL] Pillow: {e}")
    return False


# ------------------------------------------------------------
def convert_via_wand(src, dst):
    try:
        from wand.image import Image as WandImage
        print("  [INFO] Trying wand...")
        with WandImage(filename=str(src)) as img:
            img.alpha_channel = True
            img.format = "png"
            img.save(filename=str(dst))
        return True
    except ImportError:
        print("  [SKIP] wand not installed: pip install wand")
    except Exception as e:
        print(f"  [FAIL] wand: {e}")
    return False


# ------------------------------------------------------------
def convert(src, dst, texconv):
    if texconv:
        print(f"  [TRY]  texconv")
        if convert_via_texconv(src, dst, texconv):
            return True
        print("  [FALLBACK] Trying Pillow...")

    if convert_via_pillow(src, dst):
        return True

    print("  [FALLBACK] Trying wand...")
    return convert_via_wand(src, dst)


# ------------------------------------------------------------
def main():
    texconv = find_texconv()
    if texconv:
        print(f"[OK]   texconv found: {texconv}\n")
    else:
        print("[WARN] texconv.exe NOT found!")
        print("       Get it: https://github.com/microsoft/DirectXTex/releases")
        print("       -> Assets -> texconv.exe")
        print("       Put texconv.exe next to this script.\n")

    dds_files = sorted(SCRIPT_DIR.glob(f"*{INPUT_EXT}"))

    if not dds_files:
        print(f"[INFO] No {INPUT_EXT} files found in:\n       {SCRIPT_DIR}")
        return

    print(f"[INFO] Found {len(dds_files)} DDS file(s)\n")

    ok, skip, fail = 0, 0, 0

    for src in dds_files:
        dst = src.with_suffix(OUTPUT_EXT)
        print(f"-> {src.name}")

        if SKIP_EXISTING and dst.exists():
            print(f"  [SKIP] already exists.")
            skip += 1
            continue

        if convert(src, dst, texconv):
            size_kb = dst.stat().st_size / 1024
            print(f"  [OK]   {dst.name}  ({size_kb:.1f} KB)")
            ok += 1
        else:
            fail += 1

        print()

    tmp = SCRIPT_DIR / "_texconv_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 50)
    print(f"  Done.  OK={ok}  Skipped={skip}  Failed={fail}")
    print("=" * 50)


if __name__ == "__main__":
    main()