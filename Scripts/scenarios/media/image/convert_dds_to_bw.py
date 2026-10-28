"""
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
DDS2BW.py  v6.2 (The "Green Killer" Edition)
===================================================
Batch converts ALL .dds files next to this script to pure Black & White.
No green tints, no missing arguments, no bullshit.

REQUIREMENTS:
  pip install Pillow numpy
  texconv.exe (next to script or in PATH)
"""

import sys
import shutil
import subprocess
import tempfile
import re
from pathlib import Path

try:
    from PIL import Image, ImageEnhance
except ImportError:
    print("[ERROR] Pillow not found. Run: pip install Pillow numpy")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("[ERROR] numpy not found. Run: pip install Pillow numpy")
    sys.exit(1)

# ═════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ═════════════════════════════════════════════════════════════════════════════
LEVEL_BLACK = 0       # 0..254
LEVEL_WHITE = 255      # 1..255
SCURVE = 0.1           # 0.0..1.0
GAMMA = 2.0            # 0.1..5.0
SHADOWS    = 0         # -100..100
HIGHLIGHTS = 0         # -100..100
BRIGHTNESS = 1.0       
CONTRAST   = 1.1      
SUFFIX  = ""           # e.g., "_bw"
FORMAT  = "AUTO"       # "AUTO" uses BC4 for Grayscale, BC3 for Alpha
SRGB    = False        
DRY_RUN = False        
# ═════════════════════════════════════════════════════════════════════════════

def safe_stem(stem: str, index: int) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned if cleaned else f"file_{index:04d}"

def find_texconv(script_dir: Path) -> Path:
    local = script_dir / "texconv.exe"
    if local.exists(): return local
    found = shutil.which("texconv") or shutil.which("texconv.exe")
    if found: return Path(found)
    raise FileNotFoundError("texconv.exe not found! Put it next to the script.")

def dds_to_png(texconv: Path, dds_path: Path, out_dir: Path, safe_name: str) -> Path:
    safe_dds = out_dir / (safe_name + ".dds")
    shutil.copy2(str(dds_path), str(safe_dds))
    cmd = [str(texconv), "-ft", "png", "-o", str(out_dir), "-y", str(safe_dds)]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return out_dir / (safe_name + ".png")

def png_to_dds(texconv: Path, png_path: Path, out_dir: Path, has_alpha: bool) -> Path:
    if FORMAT == "AUTO":
        # BC4 is pure 1-channel luminance (perfect for B&W). BC3 is for Alpha.
        fmt = "BC3_UNORM" if has_alpha else "BC4_UNORM"
    else:
        fmt = FORMAT
    
    cmd = [str(texconv), "-ft", "dds", "-f", fmt, "-o", str(out_dir), "-y", str(png_path)]
    if SRGB: cmd += ["-srgb"]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return out_dir / (png_path.stem + ".dds")

def process_image(png_path: Path) -> bool:
    img = Image.open(png_path)
    alpha = None
    
    # 1. Стерилизация каналов: выдираем альфу и превращаем остальное в честный серый (L)
    if "A" in img.mode or img.mode == "P":
        img = img.convert("RGBA")
        alpha = np.array(img)[:, :, 3]
        gray_img = img.convert("L")
    else:
        gray_img = img.convert("L")

    v = np.array(gray_img, dtype=np.float32) / 255.0

    # 2. Математика контраста и уровней
    if LEVEL_BLACK != 0 or LEVEL_WHITE != 255:
        lo, hi = LEVEL_BLACK / 255.0, LEVEL_WHITE / 255.0
        v = np.clip((v - lo) / (max(hi, lo + 0.001) - lo), 0.0, 1.0)

    if GAMMA != 1.0: 
        v = np.power(np.clip(v, 1e-6, 1.0), GAMMA)

    if SCURVE > 0.0:
        v = v * (1.0 - SCURVE) + (v * v * (3.0 - 2.0 * v)) * SCURVE

    final_gray = Image.fromarray((np.clip(v, 0.0, 1.0) * 255.0).astype(np.uint8), "L")
    
    if BRIGHTNESS != 1.0: final_gray = ImageEnhance.Brightness(final_gray).enhance(BRIGHTNESS)
    if CONTRAST   != 1.0: final_gray = ImageEnhance.Contrast(final_gray).enhance(CONTRAST)

    # 3. Сохранение в одноканальный режим (L или LA), чтобы убить риск цветовых артефактов
    if alpha is not None:
        final_img = Image.merge("LA", [final_gray, Image.fromarray(alpha, "L")])
        final_img.save(png_path)
        return True
    else:
        final_gray.save(png_path)
        return False

def resolve_target():
    """T-166 launch contract: the launcher's positional target is the authority.

    INVALID explicit target must refuse loudly: nonzero exit, no mutation,
    no silent fallback to CWD/script directory.
    """
    if len(sys.argv) < 2:
        return None
    t = Path(sys.argv[1]).expanduser().resolve()
    if not t.is_dir():
        print(f"[REFUSE] target is not an existing folder: {t}", file=sys.stderr)
        sys.exit(2)
    return t


def main():
    script_dir = resolve_target() or Path(__file__).parent.resolve()
    dds_files  = sorted(script_dir.glob("*.dds"))
    if not dds_files:
        print("[INFO] No .dds files found. Check the folder, soldier.")
        return

    try:
        texconv = find_texconv(script_dir)
        print(f"[OK] Using texconv: {texconv}")
    except FileNotFoundError as e:
        print(f"[ERROR] {e}"); sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for i, dds in enumerate(dds_files):
            sn = safe_stem(dds.stem, i)
            print(f"  Processing: {dds.name}", end=" ", flush=True)
            try:
                # Шаг 1: DDS -> PNG
                png = dds_to_png(texconv, dds, tmp_path, sn)
                # Шаг 2: Обесцвечивание (возвращает True если есть альфа)
                has_alpha = process_image(png)
                # Шаг 3: PNG -> DDS
                dds_out = png_to_dds(texconv, png, tmp_path, has_alpha)
                
                final_name = dds.stem + SUFFIX + ".dds"
                shutil.move(str(dds_out), str(script_dir / final_name))
                print(f"-> {final_name} [DONE]")
            except Exception as e:
                print(f"\n    [FAIL] Something went sideways with {dds.name}: {e}")

    print("\n[FINISH] All tasks completed. Now get out of here.")

if __name__ == "__main__":
    main()