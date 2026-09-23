"""Mute the first seconds of every .wav in the target folder.

T-166 launch contract: the launcher's positional target is the authority.
INVALID explicit target must refuse loudly: nonzero exit, no mutation,
no silent fallback to CWD/script directory.

Safety contract (E-1283 repair):
- duration and frame count preserved EXACTLY: only the first
  min(duration, file duration) seconds are replaced with silence,
  all remaining frames are copied byte-identical.
- file.wav.bak is written once, byte-identical, BEFORE any modification;
  if a backup already exists that file is REFUSED (BACKUP_EXISTS) so the
  only pristine copy is never overwritten. Other files continue.
- writes go through a temp file published atomically (os.replace);
  the original is never opened for destructive write before a valid
  replacement exists.
"""
import os
import sys
import wave
import hashlib
import shutil
from pathlib import Path


def resolve_target():
    if len(sys.argv) < 2:
        return None
    t = Path(sys.argv[1]).expanduser().resolve()
    if not t.is_dir():
        print(f"[REFUSE] target is not an existing folder: {t}", file=sys.stderr)
        sys.exit(2)
    return t


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def mute_wav(filepath, duration=1.0):
    """Silence the first `duration` seconds in place. Returns status string."""
    p = Path(filepath)
    bak = p.with_suffix(".wav.bak")

    if bak.exists():
        return f"BACKUP_EXISTS {p.name}"

    # Copy params first; only then create the pristine backup.
    with wave.open(str(p), "rb") as w:
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        framerate = w.getframerate()
        nframes = w.getnframes()
        comp = w.getcomptype()
        compname = w.getcompname()

    mute_frames = int(round(min(duration, nframes / float(framerate)) * framerate))
    if mute_frames > nframes:
        mute_frames = nframes
    mute_bytes = mute_frames * channels * sampwidth

    tmp = p.with_suffix(".wav.tmp")
    try:
        with wave.open(str(p), "rb") as src, wave.open(str(tmp), "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(sampwidth)
            out.setframerate(framerate)
            if comp != "NONE":
                out.setcomptype(comp, compname)
            out.writeframes(b"\x00" * mute_bytes)
            # Skip muted frames in source
            if mute_frames > 0:
                src.setpos(mute_frames)
            remaining_frames = nframes - mute_frames
            if remaining_frames > 0:
                chunk = 1 << 20
                for _ in range(0, remaining_frames, chunk):
                    out.writeframes(src.readframes(min(chunk, remaining_frames)))
        # Backup only after the replacement exists: byte-identical original.
        shutil.copy2(str(p), str(bak))
        os.replace(str(tmp), str(p))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return f"OK {p.name}"


def main():
    folder = resolve_target() or Path(__file__).resolve().parent
    wavs = sorted(folder.glob("*.wav"))

    if not wavs:
        print(f"No .wav files found in: {folder}")
        return

    print(f"Found {len(wavs)} file(s). Muting first second...")
    refused = 0
    for w in wavs:
        try:
            status = mute_wav(w, duration=1.0)
        except Exception as e:
            status = f"FAIL {w.name}: {e}"
        print(f"  {status}")
        if not status.startswith("OK"):
            refused += 1
    print(f"All done. OK={len(wavs) - refused} refused/failed={refused}")
    if refused > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
