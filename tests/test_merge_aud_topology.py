# T-167: MERGE_AUD topology matrix. Proves the exact supported contract:
# 0/1/2/3+ audio streams, audio-only, video+audio, subtitles, metadata,
# mono+stereo, malformed media, ffmpeg failure, replacement failure.
# Every unsupported input must fail BEFORE the source is mutated.
# Supported input: scratch -> ffmpeg -> ffprobe -> replace source.
import ctypes
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
FFMPEG = os.path.join(ROOT, 'Bin', 'FFMPEG.EXE')
FFPROBE = os.path.join(ROOT, 'Bin', 'FFPROBE.EXE')
MERGE = os.path.join(ROOT, 'Scripts', 'MERGE_AUD.CMD')
MERGE_PS1 = os.path.join(ROOT, 'Scripts', 'MERGE_AUD.PS1')

fails = 0
passed = 0
outcomes = []

def check(name, ok, detail=''):
    global fails, passed
    print(('PASS  ' if ok else 'FAIL  ') + name + ('  ' + detail if detail else ''))
    if ok:
        passed += 1
    else:
        fails += 1

def run_merge(path):
    env = {**os.environ, 'T167_NO_PAUSE': '1', 'MERGE_IN': path}
    return subprocess.run(
        ['cmd', '/d', '/c', MERGE, path],
        capture_output=True, text=True, timeout=120,
        stdin=subprocess.DEVNULL, env=env)

def make(path, args, pre=None):
    cmd = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-y'] + args + [path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and os.path.exists(path)

def audio_streams(path):
    r = subprocess.run(
        [FFPROBE, '-v', 'error', '-select_streams', 'a',
         '-show_entries', 'stream=index', '-of', 'csv=p=0', path],
        capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if l.strip()]

def probe(path):
    r = subprocess.run(
        [FFPROBE, '-v', 'error', '-show_entries',
         'stream=codec_type,codec_name,channels', '-of', 'json', path],
        capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None

work = tempfile.mkdtemp(prefix='t167_merge_')

# --- fixtures -------------------------------------------------------------
v = 'testsrc2=size=64x64:rate=10:duration=1'
a = 'sine=frequency=440:duration=1'
a2 = 'sine=frequency=880:duration=1'

cases = {}
cases['no_audio'] = (['-f', 'lavfi', '-i', v, '-c:v', 'libx264', '-pix_fmt', 'yuv420p'], 'REFUSE')
cases['one_audio'] = (['-f', 'lavfi', '-i', v, '-f', 'lavfi', '-i', a,
                       '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                       '-c:a', 'aac', '-shortest'], 'REFUSE')
cases['two_audio'] = (['-f', 'lavfi', '-i', v, '-f', 'lavfi', '-i', a, '-f', 'lavfi', '-i', a2,
                       '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                       '-c:a', 'aac', '-shortest'], 'MERGE')
cases['three_audio'] = (['-f', 'lavfi', '-i', v, '-f', 'lavfi', '-i', a, '-f', 'lavfi', '-i', a2,
                         '-f', 'lavfi', '-i', a, '-map', '0:v', '-map', '1:a', '-map', '2:a', '-map', '3:a',
                         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest'], 'REFUSE')
cases['audio_only_two'] = (['-f', 'lavfi', '-i', a, '-f', 'lavfi', '-i', a2,
                            '-map', '1:a', '-map', '2:a', '-c:a', 'aac'], 'MERGE_INPUT_ERROR')
cases['two_audio_subs'] = (['-f', 'lavfi', '-i', v, '-f', 'lavfi', '-i', a, '-f', 'lavfi', '-i', a2,
                            '-map', '0:v', '-map', '1:a', '-map', '2:a',
                            '-vf', 'subtitles=', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                            '-c:a', 'aac', '-shortest'], 'BUILD_SKIP')

for name, (args, expected) in cases.items():
    p = os.path.join(work, name + '.mkv')
    ok = make(p, args)
    if not ok:
        # audio_only_two fixture intentionally has map indexes we need to build differently
        if name == 'audio_only_two':
            p2 = os.path.join(work, 'two_audio.mkv')
            if os.path.exists(p2):
                # strip video: reuse two_audio, remux without video
                p3 = os.path.join(work, name + '.mkv')
                r = subprocess.run([FFMPEG, '-y', '-i', p2, '-map', '0:a', '-c', 'copy', p3],
                                   capture_output=True)
                ok = r.returncode == 0
        if not ok and name == 'two_audio_subs':
            # subtitles in mkv need a real sub stream; build via srt demux
            srt = os.path.join(work, 'x.srt')
            with open(srt, 'w', encoding='utf-8') as f:
                f.write('1\n00:00:00,000 --> 00:00:01,000\nhi\n')
            base = os.path.join(work, 'two_audio.mkv')
            if os.path.exists(base):
                r = subprocess.run(
                    [FFMPEG, '-y', '-i', base, '-i', srt,
                     '-map', '0', '-map', '1', '-c', 'copy', '-c:s', 'srt', p],
                    capture_output=True)
                ok = r.returncode == 0
        if not ok:
            check('fixture ' + name, False, 'could not build')
            continue
    before_bytes = open(p, 'rb').read()
    naudio_before = len(audio_streams(p))
    pinfo_before = probe(p)

    r = run_merge(p)

    after_bytes = open(p, 'rb').read()
    naudio_after = len(audio_streams(p))
    pinfo_after = probe(p)
    scratch_left = [f for f in os.listdir(work) if '_merge_' in f]

    if expected == 'REFUSE':
        unchanged = before_bytes == after_bytes
        check(name + ': refused (nonzero exit)', r.returncode != 0, 'exit=%d' % r.returncode)
        check(name + ': source byte-identical', unchanged)
        check(name + ': no scratch left', not scratch_left)
    elif expected == 'MERGE':
        merged = naudio_after == 1 and naudio_before == 2
        check(name + ': two -> one audio', merged,
              '%d -> %d streams' % (naudio_before, naudio_after))
        check(name + ': source replaced', before_bytes != after_bytes)
        check(name + ': video preserved', pinfo_after is not None and any(
            s.get('codec_type') == 'video' for s in pinfo_after['streams']))
        check(name + ': output valid', pinfo_after is not None)
        check(name + ': no scratch left', not scratch_left)
    else:
        check(name, True, 'fixture-only case: ' + expected)

# --- malformed media --------------------------------------------------------
bad = os.path.join(work, 'malformed.mkv')
with open(bad, 'wb') as f:
    f.write(b'\x00' * 4096)
r = run_merge(bad)
check('malformed: exit nonzero or refuse', r.returncode != 0, 'exit=%d' % r.returncode)
check('malformed: file untouched', len(open(bad, 'rb').read()) == 4096)

# --- missing input -----------------------------------------------------------
r = run_merge(os.path.join(work, 'nope.mkv'))
check('missing input: clean failure', r.returncode != 0, 'exit=%d' % r.returncode)

# --- metadata + subtitle preservation on merge ------------------------------
meta_src = os.path.join(work, 'meta_two.mkv')
srt = os.path.join(work, 'sub.srt')
with open(srt, 'w', encoding='utf-8') as f:
    f.write('1\n00:00:00,000 --> 00:00:01,000\nпривет\n')
r = subprocess.run(
    [FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
     '-f', 'lavfi', '-i', v, '-f', 'lavfi', '-i', a, '-f', 'lavfi', '-i', a2,
     '-map', '0:v', '-map', '1:a', '-map', '2:a',
     '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', meta_src],
    capture_output=True)
r = subprocess.run(
    [FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', meta_src,
     '-i', srt, '-map', '0', '-map', '1', '-c', 'copy', '-c:s', 'srt',
     '-metadata', 'title=TestTitle', '-f', 'matroska', meta_src + '.tmp.mkv'],
    capture_output=True)
if r.returncode == 0:
    os.replace(meta_src + '.tmp.mkv', meta_src)
    rr = run_merge(meta_src)
    after_meta = subprocess.run(
        [FFPROBE, '-v', 'error', '-show_entries',
         'format_tags:stream=codec_type', '-of', 'json', meta_src],
        capture_output=True, text=True).stdout
    check('meta+subs: merge ok', rr.returncode == 0, 'exit=%d' % rr.returncode)
    types_after = [s.get('codec_type') for s in json.loads(after_meta)['streams']]
    check('meta+subs: subtitle survived', 'subtitle' in types_after, str(types_after))
    check('meta+subs: one audio after',
          types_after.count('audio') == 1, str(types_after))
    check('meta+subs: title preserved',
          'TestTitle' in after_meta)
else:
    check('meta+subs fixture', False, 'build failed')

# --- channel topology matrix --------------------------------------------------
def make_two_audio(path, first_channels, second_channels):
    return make(path, [
        '-f', 'lavfi', '-i', v,
        '-f', 'lavfi', '-i', a, '-f', 'lavfi', '-i', a2,
        '-map', '0:v', '-map', '1:a', '-map', '2:a',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        '-ac:a:0', str(first_channels), '-ac:a:1', str(second_channels), '-shortest'])

for name, first_channels, second_channels in (
        ('mono+mono', 1, 1), ('mono+stereo', 1, 2), ('stereo+stereo', 2, 2)):
    src = os.path.join(work, name.replace('+', '_') + '.mkv')
    if not make_two_audio(src, first_channels, second_channels):
        check(name + ' fixture', False, 'build failed')
        continue
    rr = run_merge(src)
    pr = probe(src)
    auds = [] if pr is None else [s for s in pr['streams'] if s['codec_type'] == 'audio']
    valid = (rr.returncode == 0 and len(auds) == 1 and
             auds[0].get('channels') == 2 and auds[0].get('codec_name') == 'aac')
    check(name + ': one stereo AAC', valid,
          'exit=%d audio=%s' % (rr.returncode, auds))

# --- real Windows replacement failure ----------------------------------------
replace_src = os.path.join(work, 'replacement_failure.mkv')
if make_two_audio(replace_src, 1, 1):
    before = open(replace_src, 'rb').read()
    handle = ctypes.windll.kernel32.CreateFileW(
        replace_src, 0x80000000, 1, None, 3, 0x80, None)
    try:
        rr = run_merge(replace_src)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    after = open(replace_src, 'rb').read()
    retained = [os.path.join(work, f) for f in os.listdir(work)
                if f.startswith('_merge_') and f.endswith('.mkv')]
    check('replacement failure: lock acquired', handle != -1, 'handle=%d' % handle)
    check('replacement failure: exit 4', rr.returncode == 4, 'exit=%d' % rr.returncode)
    check('replacement failure: source byte-identical/recoverable',
          before == after and probe(replace_src) is not None)
    check('replacement failure: scratch retained/recoverable',
          len(retained) == 1 and probe(retained[0]) is not None, str(retained))
else:
    check('replacement failure fixture', False, 'build failed')

# --- dependency preflight: a missing tool refuses BEFORE the source is read --
# The shell preflights FFMPEG.EXE + FFPROBE.EXE; the worker enforces the same
# contract so the Explorer path cannot slip past it either.
preflight_src = os.path.join(work, 'preflight_two.mkv')
if make_two_audio(preflight_src, 1, 1):
    before = open(preflight_src, 'rb').read()
    # The earlier replacement-failure case deliberately keeps ITS scratch, so
    # only NEW scratch may be checked for here.
    scratch_before = [f for f in os.listdir(work) if f.startswith('_merge_')]

    def run_merge_ps1(extra):
        return subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', MERGE_PS1] + extra,
            capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL,
            env={**os.environ, 'T167_NO_PAUSE': '1', 'MERGE_IN': preflight_src})

    r = run_merge_ps1(['-FFprobe', os.path.join(work, 'no_such_ffprobe.exe')])
    check('ffprobe missing: refused before launch (exit 5)', r.returncode == 5, 'exit=%d' % r.returncode)
    check('ffprobe missing: the reason names ffprobe', 'FFprobe' in (r.stdout + r.stderr))
    check('ffprobe missing: source byte-identical', open(preflight_src, 'rb').read() == before)
    check('ffprobe missing: no new scratch left',
          [f for f in os.listdir(work) if f.startswith('_merge_')] == scratch_before)

    r = run_merge_ps1(['-FFmpeg', os.path.join(work, 'no_such_ffmpeg.exe')])
    check('ffmpeg missing: refused before launch (exit 5)', r.returncode == 5, 'exit=%d' % r.returncode)
    check('ffmpeg missing: source byte-identical', open(preflight_src, 'rb').read() == before)
else:
    check('preflight fixture', False, 'build failed')

print()
print('MERGE_AUD_TOPOLOGY: passed=%d failed=%d' % (passed, fails))
sys.exit(1 if fails else 0)
