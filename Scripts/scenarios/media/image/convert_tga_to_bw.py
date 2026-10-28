"""
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
TGA Grayscale + Upscale + Sharpen (No Antialiasing)
====================================================
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
# 2 = dvojnoj razmer / 1 = bez apskejla
SCALE_FACTOR = 2

# Krasit' v cherno-belyj?
# True  = da (no format ostajetsja RGB/RGBA -- igry ne slomajutsja)
# False = net, ostavit' originalnyj cvet
GRAYSCALE = True

# Uroven' sharpena:
#   0 = bez sharpena
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
# "_bw" = sozdat' rjadom texture_bw.tga, original zhiv
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


def to_grayscale_rgb(img):
    """
    Obescvechivanie BEZ smeny formata.
    RGB  -> RGB  (R=G=B=yarkost')
    RGBA -> RGBA (R=G=B=yarkost', A ne trogaetsja)
    Igra poluchaet tot zhe format chto i ozhidaet.
    """
    if img.mode == "RGBA":
        r, g, b, a = img.split()
        gray = Image.merge("RGB", (r, g, b)).convert("L")
        gray_rgb = gray.convert("RGB")
        return Image.merge("RGBA", (*gray_rgb.split(), a))

    elif img.mode == "RGB":
        gray = img.convert("L")
        return gray.convert("RGB")

    else:
        # P, L, i dr -- konvertiruem v RGB i obesvechem
        rgb = img.convert("RGB")
        gray = rgb.convert("L")
        return gray.convert("RGB")


def apply_sharpen(img):
    if SHARPEN_LEVEL == 0 or SHARPEN_PASSES == 0:
        return img
    if img.mode not in ("RGB", "RGBA"):
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

        # Upscale
        if SCALE_FACTOR != 1:
            new_w = int(orig_w * SCALE_FACTOR)
            new_h = int(orig_h * SCALE_FACTOR)
            img = img.resize((new_w, new_h), resample=Image.NEAREST)
        else:
            new_w, new_h = orig_w, orig_h

        # Grayscale (ostajutsja v RGB/RGBA)
        if GRAYSCALE:
            img = to_grayscale_rgb(img)

        # Sharpen
        img = apply_sharpen(img)

        # Output
        if OUTPUT_SUFFIX:
            out_path = filepath.parent / (filepath.stem + OUTPUT_SUFFIX + filepath.suffix)
        else:
            out_path = filepath

        if BACKUP and out_path == filepath:
            shutil.copy2(filepath, filepath.with_suffix(".bak.tga"))

        img.save(out_path, format="TGA")

        bw_tag = f" [BW, format: {img.mode}]" if GRAYSCALE else ""
        label  = out_path.name if out_path != filepath else "< zamena"
        print(f"  [OK]  {filepath.name:<40} {orig_w}x{orig_h} --> {new_w}x{new_h}{bw_tag}  {label}")
        return True

    except Exception as e:
        print(f"  [ERR] {filepath.name}: {e}")
        return False


def main():
    pattern = "**/*.tga" if RECURSIVE else "*.tga"
    files = sorted(HERE.glob(pattern))

    if not files:
        print(f"\n[!] .tga fajlov ne najdeno ryadom so skriptom.")
        print(f"    Papka: {HERE}")
        pause_if_interactive()
        return

    suffix_info = f"name{OUTPUT_SUFFIX}.tga" if OUTPUT_SUFFIX else "zamena originala"

    print(f"\n  Papka     : {HERE}")
    print(f"  Fajlov    : {len(files)}")
    print(f"  Apskejl   : x{SCALE_FACTOR}  (NEAREST, nol' antialiainga)")
    print(f"  Grayscale : {'da (format RGB/RGBA sohranjon)' if GRAYSCALE else 'net'}")
    print(f"  Sharpen   : uroven' {SHARPEN_LEVEL},  {SHARPEN_PASSES} pas(ov)")
    print(f"  Rekursija : {'da' if RECURSIVE else 'net'}")
    print(f"  Bakap     : {'da (.bak.tga)' if BACKUP else 'net'}")
    print(f"  Vyvod     : {suffix_info}")
    print(f"  {'-'*56}\n")

    ok   = sum(process_file(f) for f in files)
    fail = len(files) - ok

    print(f"\n  {'='*56}")
    print(f"  Gotovo!   OK: {ok}   Oshibok: {fail}")
    print(f"  {'='*56}\n")
    pause_if_interactive()


if __name__ == "__main__":
    main()
