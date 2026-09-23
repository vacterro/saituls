"""
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
PIC -> TGA Upscaler x8 (No Antialiasing, No Sharpen)
=====================================================
Polozhil skript ryadom s .pic fajlami -- zapustil -- gotovo.
Ishet .pic toljko v papke gde lezhit sam skript.
Sozdajot rjadom novyj .tga, originalnyj .pic ne trogaet.

Requirements:
    pip install Pillow
"""

from PIL import Image
import shutil
import sys
from pathlib import Path

# ================================================================
#   NASTROJKI  -  TROGAJ TOLJKO ZDES
# ================================================================

# Koefficient apskejla.
# 8 = vosemnoj razmer (32x32 -> 256x256)
SCALE_FACTOR = 8

# Udaljtj originalnyj .pic posle konversii?
# True  = da, udalit' .pic
# False = net, ostavit' rjadom
DELETE_ORIGINAL = False

# Bakap .pic pered udaleniem (rabotaet toljko esli DELETE_ORIGINAL = True)
# True  = pereimenovat' v .pic.bak vmesto udalenija
# False = udalit' sovsem
BACKUP_ORIGINAL = True

# Iskat' .pic rekursivno vo vseh podpapkah?
# True = da / False = toljko rjadom so skriptom
RECURSIVE = False

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


def process_file(filepath):
    try:
        img = Image.open(filepath)
        orig_w, orig_h = img.width, img.height
        orig_mode = img.mode

        new_w = int(orig_w * SCALE_FACTOR)
        new_h = int(orig_h * SCALE_FACTOR)

        # NEAREST = nol' antialiainga, chistyj piksel' v piksel'
        img = img.resize((new_w, new_h), resample=Image.NEAREST)

        # Esli rezhim ne podderzhivaetsja TGA -- konvertiruem
        if orig_mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGBA" if "A" in orig_mode else "RGB")

        out_path = filepath.with_suffix(".tga")
        img.save(out_path, format="TGA")

        # Obrabotka originala
        if DELETE_ORIGINAL:
            if BACKUP_ORIGINAL:
                bak_path = filepath.with_suffix(".pic.bak")
                shutil.move(str(filepath), str(bak_path))
                orig_tag = f"(original -> .pic.bak)"
            else:
                filepath.unlink()
                orig_tag = f"(original udaljon)"
        else:
            orig_tag = "(original sohranjon)"

        print(f"  [OK]  {filepath.name:<40} {orig_w}x{orig_h} --> {new_w}x{new_h}  -> {out_path.name}  {orig_tag}")
        return True

    except Exception as e:
        print(f"  [ERR] {filepath.name}: {e}")
        return False


def main():
    pattern = "**/*.pic" if RECURSIVE else "*.pic"
    files = sorted(HERE.glob(pattern))

    if not files:
        print(f"\n[!] .pic fajlov ne najdeno.")
        print(f"    Papka: {HERE}")
        pause_if_interactive()
        return

    if DELETE_ORIGINAL:
        orig_fate = ".pic.bak (pereimenovan)" if BACKUP_ORIGINAL else "UDALJOT'SJA"
    else:
        orig_fate = "ostajutsja rjadom"

    print(f"\n  Papka     : {HERE}")
    print(f"  Fajlov    : {len(files)}")
    print(f"  Apskejl   : x{SCALE_FACTOR}  (NEAREST, nol' antialiainga)")
    print(f"  Sharpen   : net")
    print(f"  Format    : .pic -> .tga")
    print(f"  Originaly : {orig_fate}")
    print(f"  Rekursija : {'da' if RECURSIVE else 'net'}")
    print(f"  {'-'*56}\n")

    ok   = sum(process_file(f) for f in files)
    fail = len(files) - ok

    print(f"\n  {'='*56}")
    print(f"  Gotovo!   OK: {ok}   Oshibok: {fail}")
    print(f"  {'='*56}\n")
    pause_if_interactive()


if __name__ == "__main__":
    main()
