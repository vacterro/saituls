# ============================================================
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
#  PNG2DDS.py  v2
#
#  РЕЖИМ 1 (PNG найдены):
#    PNG -> DDS, формат задаётся DDS_FORMAT ниже
#
#  РЕЖИМ 2 (PNG не найдены, но есть DDS):
#    DDS -> DDS, читает оригинальный формат из заголовка
#    и перепаковывает в тот же формат через texconv
#
#  GET texconv.exe:
#    https://github.com/microsoft/DirectXTex/releases
#    -> Assets -> texconv.exe  (положи рядом со скриптом)
# ============================================================

import os
import struct
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


SCRIPT_DIR    = resolve_target() or Path(__file__).resolve().parent
FOLDER_MODE   = len(sys.argv) > 1
SKIP_EXISTING = False

# Формат по умолчанию для PNG -> DDS
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

# DXGI enum -> texconv строка
DXGI_TO_STR = {
    2:  "R32G32B32A32_FLOAT",
    10: "R16G16B16A16_FLOAT",
    28: "R8G8B8A8_UNORM",
    29: "R8G8B8A8_UNORM_SRGB",
    41: "R8_UNORM",
    71: "BC1_UNORM",
    72: "BC1_UNORM_SRGB",
    74: "BC2_UNORM",
    75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM",
    78: "BC3_UNORM_SRGB",
    80: "BC4_UNORM",
    83: "BC5_UNORM",
    95: "BC6H_UF16",
    96: "BC6H_SF16",
    98: "BC7_UNORM",
    99: "BC7_UNORM_SRGB",
    87: "B8G8R8A8_UNORM",
    91: "B8G8R8A8_UNORM_SRGB",
}

# FourCC старых DDS -> texconv строка
FOURCC_TO_STR = {
    b"DXT1": "BC1_UNORM",
    b"DXT3": "BC2_UNORM",
    b"DXT5": "BC3_UNORM",
    b"ATI1": "BC4_UNORM",
    b"BC4U": "BC4_UNORM",
    b"ATI2": "BC5_UNORM",
    b"BC5U": "BC5_UNORM",
}


# ------------------------------------------------------------
def detect_dds_format(path):
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"DDS ":
                return DDS_FORMAT
            f.seek(76)                          # DDPF offset
            f.read(4)                           # dwSize
            f.read(4)                           # dwFlags
            fourcc = f.read(4)

            if fourcc == b"DX10":
                f.seek(128)                     # DX10 extended header
                dxgi_val = struct.unpack("<I", f.read(4))[0]
                return DXGI_TO_STR.get(dxgi_val, str(dxgi_val))

            fmt = FOURCC_TO_STR.get(fourcc)
            if fmt:
                return fmt

            f.seek(88)                          # dwRGBBitCount
            bpp = struct.unpack("<I", f.read(4))[0]
            return "B8G8R8A8_UNORM" if bpp == 32 else DDS_FORMAT

    except Exception as e:
        print(f"  [WARN] detect format failed: {e} -> fallback {DDS_FORMAT}")
        return DDS_FORMAT


# ------------------------------------------------------------
def find_texconv():
    for p in _texconv_candidates():
        p = Path(str(p))
        if p.exists():
            return p
    return None


# ------------------------------------------------------------
def run_texconv(src, dst, texconv, fmt):
    tmp_dir = SCRIPT_DIR / "_texconv_tmp"
    tmp_dir.mkdir(exist_ok=True)

    cmd = [
        str(texconv),
        "-ft", dst.suffix.lstrip("."),
        "-f",  fmt,
        "-y",
        "-o",  str(tmp_dir),
        str(src)
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60
        )
        tmp_out = tmp_dir / (src.stem + dst.suffix)
        if tmp_out.exists():
            shutil.move(str(tmp_out), str(dst))
            return True
        err = result.stderr.decode(errors="replace").strip()
        out = result.stdout.decode(errors="replace").strip()
        print(f"  [FAIL] texconv: {err or out or 'no output produced'}")
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
def process_files(files, texconv, src_ext, dst_ext, get_fmt):
    ok, skip, fail = 0, 0, 0
    for src in files:
        dst = src.with_suffix(dst_ext)
        print(f"-> {src.name}")

        if SKIP_EXISTING and dst.exists():
            print(f"  [SKIP] {dst.name} already exists.")
            skip += 1
            print()
            continue

        fmt = get_fmt(src)
        print(f"  [INFO] format: {fmt}")

        if texconv and run_texconv(src, dst, texconv, fmt):
            print(f"  [OK]   {dst.name}  ({dst.stat().st_size/1024:.1f} KB)")
            ok += 1
        else:
            print(f"  [FAIL] {src.name}")
            fail += 1
        print()
    return ok, skip, fail


# ------------------------------------------------------------
def main():
    texconv = find_texconv()
    if texconv:
        print(f"[OK]   texconv: {texconv}\n")
    else:
        print("[WARN] texconv.exe NOT found!")
        print("       https://github.com/microsoft/DirectXTex/releases")
        print("       Положи texconv.exe рядом со скриптом.\n")

    png_files = sorted(SCRIPT_DIR.glob("*.png"))
    dds_files = sorted(SCRIPT_DIR.glob("*.dds"))

    if png_files:
        print(f"[MODE] PNG -> DDS  (format: {DDS_FORMAT})")
        print(f"[INFO] Found {len(png_files)} PNG file(s)\n")
        ok, skip, fail = process_files(
            png_files, texconv,
            src_ext=".png", dst_ext=".dds",
            get_fmt=lambda _: DDS_FORMAT
        )

    elif dds_files:
        print(f"[MODE] PNG не найдены -> DDS repack с оригинальным форматом")
        print(f"[INFO] Found {len(dds_files)} DDS file(s)\n")
        ok, skip, fail = process_files(
            dds_files, texconv,
            src_ext=".dds", dst_ext=".dds",
            get_fmt=detect_dds_format
        )

    else:
        print(f"[INFO] Ни PNG ни DDS не найдено в:\n       {SCRIPT_DIR}")
        return

    tmp = SCRIPT_DIR / "_texconv_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 50)
    print(f"  Done.  OK={ok}  Skipped={skip}  Failed={fail}")
    print("=" * 50)


if __name__ == "__main__":
    main()
