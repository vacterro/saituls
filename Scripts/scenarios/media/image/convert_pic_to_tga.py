"""
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
SOFTIMAGE PIC -> TGA  |  BATTLEZONE 98  |  FINAL CORRECT VERSION
=================================================================
Magic: 0x5380F634

THE KEY DISCOVERY (from binary forensics on real asteroid.pic):
  - chained=1 means channels interleave PER SCANLINE, not per image
  - Each row stores: RGB_RLE(width px) then A_RLE(width px) back-to-back
  - NOT two big blocks [all RGB rows] then [all A rows]
  - No row-boundary clipping in RLE! Each unit is decoded freely
    until exactly `width` pixels are consumed.

Tested: asteroid.pic -> 65024 bytes consumed, 0 bytes left, 0 stripe artifacts.
"""

import struct
import sys
from pathlib import Path
from PIL import Image

# ================================================================
SCALE_FACTOR   = 8            # 1 = no upscale
UPSCALE_FILTER = Image.LANCZOS  # NEAREST | BILINEAR | BICUBIC | LANCZOS
MAGIC          = 0x5380F634
# ================================================================


def rb16(d, o): return struct.unpack_from(">H", d, o)[0]
def rbf (d, o): return struct.unpack_from(">f", d, o)[0]


def rle_row(data, i, width, unit_size):
    """
    Decode exactly `width` pixels of Softimage PIC RLE.
    NO row-boundary clipping -- reads runs freely until width*unit_size
    bytes are accumulated. This is critical: clipping literal runs
    causes byte-offset drift on subsequent rows.

    flag < 128 : literal (flag+1) pixels
    flag >= 128: run of (flag-127) copies of next pixel
    """
    out = bytearray()
    target = width * unit_size
    while len(out) < target and i < len(data):
        flag = data[i]; i += 1
        if flag < 128:
            count = flag + 1
            chunk = data[i:i + count * unit_size]
            out.extend(chunk)
            i += count * unit_size
        else:
            count = flag - 127
            unit  = bytes(data[i:i + unit_size])
            i += unit_size
            out.extend(unit * count)
    # Pad if file is truncated
    if len(out) < target:
        out.extend(b"\x00" * (target - len(out)))
    return bytes(out[:target]), i


CH_BITS = [(0x80, 0, "R"), (0x40, 1, "G"), (0x20, 2, "B"),
           (0x10, 3, "A"), (0x08, 3, "X")]

def mask_slots(mask):
    return [slot for bit, slot, _ in CH_BITS if mask & bit]

def mask_str(mask):
    return "".join(n for bit, _, n in CH_BITS if mask & bit) or "?"


def load_pic(filepath):
    data = filepath.read_bytes()

    if len(data) < 92:
        raise ValueError("Too small to be a valid Softimage PIC")
    if struct.unpack_from(">I", data, 0)[0] != MAGIC:
        raise ValueError(f"Wrong magic: {data[:4].hex().upper()}")

    version = rbf(data, 4)
    comment = data[8:88].rstrip(b"\x00").decode("ascii", errors="replace").strip()

    # Image descriptor at 0x58
    off = 88
    if data[off:off+4] != b"PICT":
        raise ValueError(f"No PICT tag at 0x58 (got {data[off:off+4]!r})")
    off += 4

    width  = rb16(data, off); off += 2
    height = rb16(data, off); off += 2
    ratio  = rbf (data, off); off += 4
    fields = rb16(data, off); off += 2
    off   += 2  # padding

    if not (0 < width <= 4096 and 0 < height <= 4096):
        raise ValueError(f"Bad dimensions {width}x{height}")

    print(f"  PIC v{version:.2f}  {width}x{height}  "
          f"ratio={ratio:.2f}  fields={fields}")
    if comment:
        print(f"  Comment: {comment!r}")

    # Read channel packets
    # chained=1 means this packet's stream and the next packet's stream
    # are interleaved PER SCANLINE: for each row, decode rgb_row then a_row.
    packets = []
    while True:
        chained  = data[off];        off += 1
        bits     = data[off];        off += 1
        type_raw = rb16(data, off);  off += 2
        comp     = (type_raw >> 8) & 0xFF
        mask     = type_raw & 0xFF
        slots    = mask_slots(mask)
        unit     = len(slots) * (bits // 8)

        print(f"  Pkt: [{mask_str(mask):4s}]  comp=0x{comp:02X}  "
              f"unit={unit}B  chained={chained}")
        packets.append({"comp": comp, "mask": mask, "slots": slots,
                        "unit": unit, "bits": bits})
        if chained == 0:
            break

    print(f"  Pixel data at 0x{off:04X}  ({len(data)-off} bytes)")

    # Build RGBA buffer, default alpha=255
    rgba = bytearray(width * height * 4)
    for p in range(width * height):
        rgba[p * 4 + 3] = 255

    # Decode: per scanline, decode each packet in order
    # (chained packets interleave at the scanline level)
    i = off
    for row in range(height):
        base = row * width * 4
        for pkt in packets:
            if pkt["unit"] == 0:
                continue
            raw, i = rle_row(data, i, width, pkt["unit"])
            # Scatter bytes into RGBA
            bpc   = pkt["bits"] // 8
            n_ch  = len(pkt["slots"])
            for x in range(width):
                for ci, rgba_idx in enumerate(pkt["slots"]):
                    src = x * n_ch * bpc + ci * bpc
                    if src < len(raw):
                        rgba[base + x * 4 + rgba_idx] = raw[src]

    consumed = i - off
    total    = len(data) - off
    print(f"  Consumed: {consumed}/{total} bytes  leftover={total-consumed}")

    return Image.frombytes("RGBA", (width, height), bytes(rgba)), width, height


def process_file(filepath):
    try:
        img, w, h = load_pic(filepath)

        # Auto-detect if alpha is actually used
        alpha_vals = img.split()[3].getextrema()
        has_alpha  = alpha_vals != (255, 255)

        if SCALE_FACTOR > 1:
            img = img.resize(
                (w * SCALE_FACTOR, h * SCALE_FACTOR),
                resample=UPSCALE_FILTER,
            )

        out = filepath.with_suffix(".tga")
        # Save RGBA TGA so alpha is preserved for engines that use it
        img.save(out, format="TGA")
        print(f"  -> {out.name}  "
              f"{w}x{h} -> {w*SCALE_FACTOR}x{h*SCALE_FACTOR}  "
              f"alpha={'yes' if has_alpha else 'no (all opaque)'}")
        return True

    except Exception as e:
        import traceback
        print(f"  [FAIL] {filepath.name}: {e}")
        traceback.print_exc()
        return False


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


def main():
    HERE  = resolve_target() or Path(__file__).parent
    files = sorted(HERE.glob("*.pic"))

    if not files:
        print(f"No .pic files found in: {HERE}")
        pause_if_interactive()
        return

    print(f"Softimage PIC -> TGA  |  scale={SCALE_FACTOR}x  filter={UPSCALE_FILTER}")
    print(f"Folder: {HERE}  |  Files: {len(files)}\n")

    ok = 0
    for f in files:
        print(f"[{f.name}]")
        ok += process_file(f)
        print()

    print(f"Done: {ok}/{len(files)} converted.")
    pause_if_interactive()


if __name__ == "__main__":
    main()
