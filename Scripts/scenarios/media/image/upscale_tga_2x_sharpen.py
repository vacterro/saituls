"""
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
TGA Upscaler 2x + Sharpen (No Antialiasing)
============================================
Polozhil skript ryadom s .tga fajlami -- zapustil -- gotovo.
Ishet .tga toljko v papke gde lezhit sam skript.

Requirements:
    pip install Pillow
"""

from PIL import Image, ImageFilter
import shutil
import sys
from pathlib import Path

# ================================================================
#   NASTROJKI  -  TROGAJ TOLJKO ZDES
# ================================================================

# Koefficient apskejla.
# 2 = dvojnoj razmer, 4 = chetverjnoj
SCALE_FACTOR = 2

# Uroven' sharpena:
#   1 = mjagkij
#   2 = srednij  <-- rekomenduju
#   3 = zhestkij
SHARPEN_LEVEL = 0

# Kolichestvo prohodov sharpena podryad.
# 1 = normalno / 2 = esli tekstury ochen' myljnye
SHARPEN_PASSES = 0

# Iskat' .tga rekursivno vo vseh podpapkah?
# True = da / False = toljko rjadom so skriptom
RECURSIVE = False

# Bakap originala pered zamenoj? (.bak.tga rjadom)
# True = da / False = net
BACKUP = False

# Suffiks k imeni fajla v vyhode.
# "" = zamenjat' original na meste
# "_2x" = sozdat' rjadom texture_2x.tga, original zhiv
OUTPUT_SUFFIX = ""

# ================================================================
#   KONEC NASTROEK  -  NIZHE NE LEZJ
# ================================================================


def resolve_target():
    """T-166 launch contract: the launcher's positional target is the authority."""
    if len(sys.argv) < 2:
        return None
    t = Path(sys.argv[1]).expanduser().resolve()
    if not t.is_dir():
        print(f"[REFUSE] target is not an existing folder: {t}", file=sys.stderr)
        raise SystemExit(2)
    return t


def pause_if_interactive():
    # Launched by SCENARIOS with an explicit target -> noninteractive.
    if len(sys.argv) >= 2:
        return
    try:
        input()
    except EOFError:
        pass


HERE = resolve_target() or Path(__file__).parent

SHARPEN_KERNELS = {
    1: ImageFilter.SHARPEN,
    2: ImageFilter.Kernel(
        size=(3, 3),
        kernel=[-1, -1, -1,
                -1,  9, -1,
                -1, -1, -1],
        scale=1, offset=0,
    ),
    3: ImageFilter.Kernel(
        size=(3, 3),
        kernel=[-2, -2, -2,
                -2, 17, -2,
                -2, -2, -2],
        scale=1, offset=0,
    ),
}


def apply_sharpen(img):
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        print(f"    [WARN] Rezhim '{img.mode}' -- sharpen propushchen.")
        return img
    kernel = SHARPEN_KERNELS.get(SHARPEN_LEVEL, ImageFilter.SHARPEN)
    for _ in range(SHARPEN_PASSES):
        if img.mode == "RGBA":
            r, g, b, a = img.split()
            rgb = Image.merge("RGB", (r, g, b)).filter(kernel)
            img = Image.merge("RGBA", (*rgb.split(), a))
        else:
            img = img.filter(kernel)
    return img


def process_file(filepath):
    try:
        img = Image.open(filepath)
        orig_w, orig_h = img.width, img.height
        new_w = int(orig_w * SCALE_FACTOR)
        new_h = int(orig_h * SCALE_FACTOR)

        img = img.resize((new_w, new_h), resample=Image.NEAREST)
        img = apply_sharpen(img)

        if OUTPUT_SUFFIX:
            out_path = filepath.parent / (filepath.stem + OUTPUT_SUFFIX + filepath.suffix)
        else:
            out_path = filepath

        if BACKUP and out_path == filepath:
            shutil.copy2(filepath, filepath.with_suffix(".bak.tga"))

        img.save(out_path, format="TGA")

        label = out_path.name if out_path != filepath else "< zamena"
        print(f"  [OK]  {filepath.name:<40} {orig_w}x{orig_h} --> {new_w}x{new_h}  {label}")
        return True

    except Exception as e:
        print(f"  [ERR] {filepath.name}: {e}")
        return False


def main():
    pattern = "**/*.tga" if RECURSIVE else "*.tga"
    files = sorted(HERE.glob(pattern))

    if not files:
        print(f"\n[!] .tga fajlov ne najdeno.")
        print(f"    Papka: {HERE}")
        pause_if_interactive()
        return

    suffix_info = f"name{OUTPUT_SUFFIX}.tga" if OUTPUT_SUFFIX else "zamena originala"

    print(f"\n  Papka     : {HERE}")
    print(f"  Fajlov    : {len(files)}")
    print(f"  Apskejl   : x{SCALE_FACTOR}  (NEAREST, nol' antialiainga)")
    print(f"  Sharpen   : uroven' {SHARPEN_LEVEL},  {SHARPEN_PASSES} pas(ov)")
    print(f"  Rekursija : {'da' if RECURSIVE else 'net'}")
    print(f"  Bakap     : {'da (.bak.tga)' if BACKUP else 'net'}")
    print(f"  Vyvod     : {suffix_info}")
    print(f"  {'-'*56}\n")

    ok = sum(process_file(f) for f in files)
    fail = len(files) - ok

    print(f"\n  {'='*56}")
    print(f"  Gotovo!   OK: {ok}   Oshibok: {fail}")
    print(f"  {'='*56}\n")
    pause_if_interactive()


if __name__ == "__main__":
    main()
