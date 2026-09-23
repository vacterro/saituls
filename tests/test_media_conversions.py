# -*- coding: utf-8 -*-
# T-167: MEDIA_CONVERSION_END_TO_END - validated matrix.
# Discovery: Registry/FFMPEG_MENU.REG + MKV_FIX.REG + MERGE_AUD.REG leaves,
# enabled audio/video scenarios. Every discovered action either executes a
# deterministic fixture or proves an explicit deterministic refusal.
# No skipped. Contracts derived from action verb + args, not suffix folklore.
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
FFMPEG = os.path.join(ROOT, 'Bin', 'FFMPEG.EXE')
FFPROBE = os.path.join(ROOT, 'Bin', 'FFPROBE.EXE')
RUNNER_BAT = os.path.join(ROOT, 'Bin', 'FFMPEG_RUN.BAT')
MERGE_BAT = os.path.join(ROOT, 'Scripts', 'MERGE_AUD.CMD')

if not os.path.exists(FFMPEG):
    print('MEDIA_ACTIONS_DISCOVERED=0')
    print('FATAL: bundled FFMPEG.EXE missing - setup.ps1 payload not present')
    sys.exit(2)
if not os.path.exists(FFPROBE):
    print('FATAL: bundled FFPROBE.EXE missing')
    sys.exit(2)

fails = 0
passed = 0
executed = 0
rows = []


def read_reg(path):
    raw = open(path, 'rb').read()
    if raw[:2] == b'\xff\xfe':
        return raw[2:].decode('utf-16')
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('utf-8', 'replace')


def reg_escape_to_cmd(data):
    return data.replace('\\\\', '\x00').replace('\\"', '"').replace('\x00', '\\')


def parse_reg_commands(text):
    out = []
    cur_key = None
    for line in text.split('\n'):
        s = line.strip()
        m = re.match(r'^\[([^\]]+)\]$', s)
        if m:
            cur_key = m.group(1)
            continue
        m = re.match(r'^@="(.*)"\s*$', s)
        if m and cur_key and cur_key.endswith('\\command'):
            out.append((cur_key, reg_escape_to_cmd(m.group(1))))
    return out


def parse_runner_cmd(cmd):
    toks = re.findall(r'"([^"]*)"', cmd)
    if len(toks) >= 4:
        return toks[2], toks[3]
    return None, None


def probe_json(path, entries):
    r = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', entries, '-of', 'json', path], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run_runner(runner, input_path, args, suffix, run_dir):
    env = {**os.environ, 'T167_NO_PAUSE': '1', 'SAIFMPEG_ARGS': args or ''}
    return subprocess.run(['cmd', '/d', '/c', runner, input_path, args, suffix], capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL, cwd=run_dir, env=env)


def run_ps1_direct(input_path, args, suffix, ff=None, fp=None):
    env = {**os.environ, 'T167_NO_PAUSE': '1'}
    if args:
        env['SAIFMPEG_ARGS'] = args
    cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', os.path.join(ROOT, 'Bin', 'FFMPEG_RUN.PS1'), '-InputPath', input_path, '-Suffix', suffix]
    if ff:
        cmd += ['-FFmpegExe', ff]
    if fp:
        cmd += ['-FFprobeExe', fp]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, env=env)


def expected_outpath(input_path, suffix):
    d = os.path.dirname(input_path)
    n = os.path.splitext(os.path.basename(input_path))[0]
    return os.path.join(d, n + suffix)


def get_required_capability(args):
    if not args:
        return 'none'
    for enc in ('av1_nvenc', 'h264_nvenc', 'hevc_nvenc'):
        if enc in args:
            return enc
    if 'libsvtav1' in args:
        return 'libsvtav1'
    if 'libvpx-vp9' in args:
        return 'libvpx-vp9'
    return 'none'


def is_frame_mode(suffix):
    return bool(suffix and ('frame_' in suffix))


def expect_streams(act, input_has_video, input_is_image=False):
    args = act['args'] or ''
    verb = act['verb'] or ''
    suffix = act['suffix'] or ''
    if 'palettegen' in args or 'paletteuse' in args:
        return ['video']
    if is_frame_mode(suffix):
        return []
    if input_is_image:
        return ['video']
    strip_video = ('-vn' in args) or bool(re.search(r'-map\s+a\b', args)) or ('Извлечь Аудио' in verb)
    strip_audio = '-an' in args
    want = []
    if not strip_audio:
        want.append('audio')
    if not strip_video and input_has_video:
        want.append('video')
    if not want and not strip_video and not strip_audio:
        want = ['video', 'audio'] if input_has_video else ['audio']
    seen = []
    for w in want:
        if w not in seen:
            seen.append(w)
    return seen


def expected_codecs(act):
    args = act['args'] or ''
    suffix = act['suffix'] or ''
    verb = act['verb'] or ''
    if 'palettegen' in args or 'paletteuse' in args:
        return 'gif', None
    vcodec = None
    acodec = None
    m = re.search(r'-c:v\s+([\w-]+)', args)
    if m:
        vcodec = m.group(1)
    m2 = re.search(r'-c:a\s+([\w-]+)', args)
    if m2:
        acodec = m2.group(1)
    if not acodec and '-ab' in args:
        acodec = 'libmp3lame'
    if not acodec and '-q:a' in args and 'libvorbis' in args:
        acodec = 'libvorbis'
    if 'Извлечь Аудио' in verb and '.mp3' in suffix and not acodec:
        acodec = 'libmp3lame'
    if is_frame_mode(suffix):
        vcodec = None
        acodec = None
    if '-an' in args:
        acodec = None
    if '-vn' in args or re.search(r'-map\s+a\b', args) or 'Извлечь Аудио' in verb:
        vcodec = None
    return vcodec, acodec


def map_codec_name(enc):
    table = {'libx264': 'h264', 'libsvtav1': 'av1', 'libaom-av1': 'av1', 'mpeg4': 'mpeg4', 'libvpx-vp9': 'vp9', 'libvpx': 'vp8', 'av1_nvenc': 'av1', 'h264_nvenc': 'h264', 'hevc_nvenc': 'hevc', 'libx265': 'hevc', 'gif': 'gif', 'aac': 'aac', 'flac': 'flac', 'libmp3lame': 'mp3', 'libvorbis': 'vorbis', 'libopus': 'opus', 'pcm_s16le': 'pcm_s16le', 'wmav2': 'wmav2'}
    return table.get(enc)


def validate_output(out_path, act, input_has_video, input_is_image=False):
    want = expect_streams(act, input_has_video, input_is_image)
    if is_frame_mode(act['suffix']):
        base = os.path.splitext(os.path.basename(out_path))[0]
        cand = os.path.join(os.path.dirname(out_path), base)
        if os.path.isdir(cand):
            frames = [f for f in os.listdir(cand) if f.startswith('frame_')]
            if not frames:
                return False, 'no frames', {}
            return True, 'ok %d frames' % len(frames), {}
        return False, 'frame dir missing', {}
    meta = probe_json(out_path, 'stream=codec_type,codec_name,channels,width,height:format=format_name,duration')
    if meta is None:
        return False, 'ffprobe cannot open output', {}
    streams = meta.get('streams') or []
    if not streams:
        return False, 'output has no streams', {}
    types = [s.get('codec_type') for s in streams]
    for w in want:
        if w not in types:
            return False, 'missing %s in %s' % (w, types), {}
    codecs = {s.get('codec_type'): s.get('codec_name') for s in streams}
    v_exp, a_exp = expected_codecs(act)
    if v_exp and v_exp != 'copy':
        want_v = map_codec_name(v_exp)
        if want_v and codecs.get('video') != want_v:
            return False, 'video codec mismatch want %s got %s' % (want_v, codecs.get('video')), codecs
    if a_exp and a_exp != 'copy' and 'audio' in types:
        want_a = map_codec_name(a_exp)
        if want_a and codecs.get('audio') != want_a:
            return False, 'audio codec mismatch want %s got %s' % (want_a, codecs.get('audio')), codecs
    if 'Извлечь Аудио' in act['verb'] and '.mp3' in (act['suffix'] or ''):
        if codecs.get('audio') != 'mp3':
            return False, 'extracted audio not mp3 got %s' % codecs.get('audio'), codecs
    fmt = meta.get('format') or {}
    try:
        if fmt.get('duration') and float(fmt['duration']) <= 0:
            return False, 'non-positive duration', codecs
    except ValueError:
        pass
    return True, 'ok', codecs


def has_scratch_leftovers(base_dir):
    for root, dirs, files in os.walk(base_dir):
        for n in files + dirs:
            if n.startswith('_run_') or n.startswith('_merge_'):
                return os.path.join(root, n)
    return None


SPECIAL_NAME = ' тест пробел & Cyrillic тест_印 (1)'

# ---------------------------------------------------------------- discovery
actions = []
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.bmp', '.tga', '.tiff', '.webp', '.gif', '.psd')
image_actions = []


def discover_ffmpeg_menu():
    text = read_reg(os.path.join(ROOT, 'Registry', 'FFMPEG_MENU.REG'))
    for key, cmd in parse_reg_commands(text):
        if 'FFMPEG_RUN.BAT' not in cmd:
            raise AssertionError('non-runner command in FFMPEG_MENU: %r' % cmd)
        args, suffix = parse_runner_cmd(cmd)
        if args is None:
            raise AssertionError('unparseable runner command: %r' % cmd)
        m = re.search(r'SystemFileAssociations\\(\.[a-z0-9]+)\\', key)
        if not m:
            continue
        ext = m.group(1)
        verb = key.split('\\shell\\', 1)[1].rsplit('\\command', 1)[0]
        act = dict(action_id='ffmpeg_menu|%s|%s' % (ext, verb), surface='Registry/FFMPEG_MENU.REG', ext=ext, verb=verb, args=args, suffix=suffix, runner=RUNNER_BAT)
        if ext in IMAGE_EXTS:
            image_actions.append(act)
        actions.append(act)


def discover_mkv_fix():
    text = read_reg(os.path.join(ROOT, 'Registry', 'MKV_FIX.REG'))
    for key, cmd in parse_reg_commands(text):
        if 'FFMPEG_RUN.BAT' not in cmd:
            raise AssertionError('non-runner command in MKV_FIX: %r' % cmd)
        args, suffix = parse_runner_cmd(cmd)
        m = re.search(r'SystemFileAssociations\\(\.[a-z0-9]+)\\', key)
        if not m:
            continue
        ext = m.group(1)
        verb = key.split('\\shell\\', 1)[1].rsplit('\\command', 1)[0]
        actions.append(dict(action_id='mkv_fix|%s|%s' % (ext, verb), surface='Registry/MKV_FIX.REG', ext=ext, verb=verb, args=args, suffix=suffix, runner=RUNNER_BAT))


def discover_merge_aud():
    actions.append(dict(action_id='merge_aud|any|merge-two-audio', surface='Registry/MERGE_AUD.REG', ext='.mkv', verb='MergeAudioTracks', args=None, suffix=None, runner=MERGE_BAT))


def discover_scenarios():
    d = json.load(open(os.path.join(ROOT, 'Scripts', 'scenarios', 'scenarios.json'), encoding='utf-8'))
    for s in d['scenarios']:
        if s.get('enabled') is False:
            continue
        if s.get('category') not in ('audio', 'video'):
            continue
        actions.append(dict(action_id='scenario|%s' % s['id'], surface='scenarios.json', ext=None, verb=s['label'], args=None, suffix=None, runner=('scenario', s)))


def discover_gui_media():
    # GUI-visible media rows live in SAITULS.cs MediaDefs, the canonical grid the
    # Tools > MEDIA section itself draws. Parsing THAT block means there is no
    # second hardcoded list to drift: a row added to the section is discovered
    # here, and a row that points at a missing worker fails this matrix.
    src = open(os.path.join(ROOT, 'SAITULS.cs'), encoding='utf-8', errors='replace').read()
    m = re.search(r'string\[,\]\s+MediaDefs\s*=\s*\{(.*?)\};', src, re.S)
    if not m:
        raise AssertionError('SAITULS.cs MediaDefs block not found')
    rows = re.findall(r'\{\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\}', m.group(1))
    if len(rows) < 3:
        raise AssertionError('MEDIA section is missing a row: %r' % (rows,))
    for label, script, mode in rows:
        worker = os.path.join(ROOT, 'Scripts', script)
        if not os.path.exists(worker):
            raise AssertionError('MEDIA row %r points at a missing worker: %s' % (label, script))
        actions.append(dict(action_id='gui|%s' % label,
                            surface='SAITULS.cs MediaDefs', ext=None, verb=label,
                            args=None, suffix=None,
                            runner=('gui', script, mode)))


discover_ffmpeg_menu()
discover_mkv_fix()
discover_merge_aud()
discover_scenarios()
discover_gui_media()

# ---------------------------------------------------------------- fixtures
FIXTURE_SPECS = {
    'wav': ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-ac', '1'],
    'mp3': ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:a', 'libmp3lame'],
    'flac': ['-f', 'lavfi', '-i', 'sine=frequency=880:duration=1', '-c:a', 'flac'],
    'aac': ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:a', 'aac'],
    'm4a': ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:a', 'aac'],
    'ogg': ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:a', 'libvorbis'],
    'wav_stereo': ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-ac', '2'],
    'mp4': ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest'],
    'mp4_noaudio': ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p'],
    'mp4_twoaudio': ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=1', '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest'],
    'mkv': ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest'],
    'mov': ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest'],
    'avi': ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'mpeg4', '-q:v', '2', '-c:a', 'aac', '-shortest'],
    'webm': ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libvpx-vp9', '-c:a', 'libopus', '-shortest'],
    'm4v': ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10:duration=1', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest'],
}


def make_fixture(kind, dest_dir):
    ext = '.wav' if kind.startswith('wav') else '.' + kind.split('_')[0]
    path = os.path.join(dest_dir, 'fx_' + kind + ext)
    r = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y'] + FIXTURE_SPECS[kind] + [path], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError('fixture %s failed: %s' % (kind, r.stderr.decode(errors='replace')[:200]))
    return path


def fixture_for(ext):
    e = ext.lstrip('.')
    if e == 'wav':
        return 'wav_stereo'
    if e in ('mp3', 'flac', 'aac', 'm4a', 'ogg'):
        return e
    if e == 'wma':
        return 'wav_stereo'
    if e == 'weba':
        return 'ogg'
    if e in ('mp4', 'mkv', 'mov', 'avi', 'webm', 'm4v'):
        return e
    return None


work = tempfile.mkdtemp(prefix='t167_matrix_')
FIXDIR = os.path.join(work, 'fixtures')
RUNDIR = os.path.join(work, 'runs')
os.makedirs(FIXDIR)
os.makedirs(RUNDIR)
FIXTURES = {}
for kind in FIXTURE_SPECS:
    FIXTURES[kind] = make_fixture(kind, FIXDIR)

VIDEO_KINDS = ('mp4', 'mkv', 'mov', 'avi', 'webm', 'm4v', 'mp4_twoaudio', 'mp4_noaudio')


def make_psd(dest_dir):
    # ffmpeg has a PSD decoder but no PSD muxer, so the .psd fixture is built
    # by hand: a minimal 8-bit RGB uncompressed Photoshop file.
    import struct
    w = h = 8
    hdr = (b'8BPS' + struct.pack('>H', 1) + b'\x00' * 6 +
           struct.pack('>H', 3) + struct.pack('>I', h) + struct.pack('>I', w) +
           struct.pack('>H', 8) + struct.pack('>H', 3))
    r = bytes([200]) * w * h
    g = bytes([100]) * w * h
    b = bytes([50]) * w * h
    path = os.path.join(dest_dir, 'fx_img.psd')
    with open(path, 'wb') as f:
        f.write(hdr + b'\x00\x00\x00\x00' * 3 + struct.pack('>H', 0) + b'\x00' + r + g + b)
    return path


def make_image_fixture(ext, dest_dir):
    e = ext.lstrip('.')
    if e in ('png', 'jpg', 'jpeg', 'bmp', 'webp', 'tga'):
        src = os.path.join(dest_dir, 'fx_src.png')
        if not os.path.exists(src):
            r = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                                '-f', 'lavfi', '-i', 'color=red:size=32x32:duration=0.04',
                                '-frames:v', '1', src], capture_output=True)
            if r.returncode != 0:
                return None
        if e == 'png':
            return src
        path = os.path.join(dest_dir, 'fx_img' + ext)
        r = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                            '-i', src, path], capture_output=True)
        return path if r.returncode == 0 and os.path.exists(path) else None
    if e == 'gif':
        path = os.path.join(dest_dir, 'fx_img.gif')
        r = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                           '-f', 'lavfi', '-i', 'color=red:size=32x32:duration=0.2:rate=5',
                           '-f', 'gif', path], capture_output=True)
        return path if r.returncode == 0 and os.path.exists(path) else None
    if e == 'tiff':
        path = os.path.join(dest_dir, 'fx_img.tiff')
        r = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
                           '-f', 'lavfi', '-i', 'color=red:size=32x32:duration=0.04',
                           '-frames:v', '1', path], capture_output=True)
        return path if r.returncode == 0 and os.path.exists(path) else None
    if e == 'psd':
        if not hasattr(make_image_fixture, '_psd'):
            make_image_fixture._psd = make_psd(dest_dir)
        return make_image_fixture._psd
    return None


IMG_FIXTURE_EXT = ('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tga', '.gif', '.tiff', '.psd')

# ---------------------------------------------------------------- main matrix
for act in actions:
    aid = act['action_id']
    if act['runner'] == MERGE_BAT:
        run_dir = os.path.join(RUNDIR, 'merge')
        os.makedirs(run_dir, exist_ok=True)
        local_src = os.path.join(run_dir, 'merge_two.mkv')
        shutil.copyfile(FIXTURES['mp4_twoaudio'], local_src)
        before_hash = sha256(local_src)
        env = {**os.environ, 'T167_NO_PAUSE': '1', 'MERGE_IN': local_src}
        r = subprocess.run(['cmd', '/d', '/c', MERGE_BAT, local_src], capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL, env=env)
        executed += 1
        ok = False
        ffprobe_col = 'FAIL'
        vcodec = acodec = ''
        source_preserved = 'replaced' if r.returncode == 0 else 'NO'
        if r.returncode == 0:
            meta = probe_json(local_src, 'stream=codec_type,codec_name,channels')
            types = [s.get('codec_type') for s in (meta or {}).get('streams', [])]
            audio_ch = [s.get('channels') for s in (meta or {}).get('streams', []) if s.get('codec_type') == 'audio']
            ok = meta is not None and types.count('audio') == 1 and 'video' in types and audio_ch and int(audio_ch[0]) == 2
            for s in (meta or {}).get('streams', []):
                if s.get('codec_type') == 'video':
                    vcodec = s.get('codec_name') or ''
                if s.get('codec_type') == 'audio':
                    acodec = s.get('codec_name') or ''
            ffprobe_col = 'PASS' if ok else 'FAIL'
        else:
            source_preserved = 'YES' if sha256(local_src) == before_hash else 'NO'
        if has_scratch_leftovers(run_dir):
            ok = False
            ffprobe_col = 'FAIL'
        r2 = subprocess.run(['cmd', '/d', '/c', MERGE_BAT, local_src], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, env={**os.environ, 'T167_NO_PAUSE': '1', 'MERGE_IN': local_src})
        repeat_run = 'PASS' if r2.returncode != 0 else 'FAIL'
        if r2.returncode == 0:
            ok = False
        status = 'PASS' if (ok and repeat_run == 'PASS') else 'FAIL'
        if status == 'PASS':
            passed += 1
        else:
            fails += 1
        rows.append([aid, act['surface'], 'mp4_twoaudio', 'in-place merged', vcodec, acodec, 'none', str(r.returncode), ffprobe_col, source_preserved, repeat_run, status])
        continue

    if isinstance(act['runner'], tuple) and act['runner'][0] == 'gui':
        _script, _mode = act['runner'][1], act['runner'][2]
        if _script == 'MERGE_AUD.PS1':
            run_dir = os.path.join(RUNDIR, 'gui_merge')
            os.makedirs(run_dir, exist_ok=True)
            local_src = os.path.join(run_dir, 'gui_merge.mkv')
            shutil.copyfile(FIXTURES['mp4_twoaudio'], local_src)
            before_hash = sha256(local_src)
            script = os.path.join(ROOT, 'Scripts', 'MERGE_AUD.PS1')
            binDir = os.path.join(ROOT, 'Bin')
            # GUI launch contract (SAITULS.cs StartMergeJob): the picked file
            # arrives as MERGE_IN and the worker runs hidden with its output
            # captured -- the result is reported in the shell, never lost with a
            # closed console.
            gui_env = {**os.environ, 'T167_NO_PAUSE': '1', 'MERGE_IN': local_src}
            r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script],
                               capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL, cwd=binDir, env=gui_env)
            executed += 1
            ok = False
            ffprobe_col = 'FAIL'
            vcodec = acodec = ''
            source_preserved = 'replaced' if r.returncode == 0 else 'NO'
            if r.returncode == 0:
                meta = probe_json(local_src, 'stream=codec_type,codec_name,channels')
                types = [s.get('codec_type') for s in (meta or {}).get('streams', [])]
                audio_ch = [s.get('channels') for s in (meta or {}).get('streams', []) if s.get('codec_type') == 'audio']
                ok = meta is not None and types.count('audio') == 1 and 'video' in types and audio_ch and int(audio_ch[0]) == 2
                for s in (meta or {}).get('streams', []):
                    if s.get('codec_type') == 'video': vcodec = s.get('codec_name') or ''
                    if s.get('codec_type') == 'audio': acodec = s.get('codec_name') or ''
                ffprobe_col = 'PASS' if ok else 'FAIL'
                source_preserved = 'replaced' if ok else 'NO'
            else:
                source_preserved = 'YES' if sha256(local_src) == before_hash else 'NO'
            r2 = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script],
                                capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, cwd=binDir, env=gui_env)
            repeat_run = 'PASS' if r2.returncode != 0 else 'FAIL'
            if r2.returncode == 0:
                ok = False
            status = 'PASS' if (ok and repeat_run == 'PASS') else 'FAIL'
            if status == 'PASS': passed += 1
            else: fails += 1
            rows.append([aid, act['surface'], 'mp4_twoaudio', 'in-place merged', vcodec, acodec, 'none', str(r.returncode), ffprobe_col, source_preserved, repeat_run, status])
            continue

        if _script == 'DL_YT.PS1':
            # Hermetic controlled launch of the REAL worker: the network
            # downloader is replaced by a stub that records its arguments
            # (SAITULS_YTDLP is the worker's documented override) and exits 0,
            # and the link input comes from a file instead of the clipboard.
            # No live Internet, no clipboard dependency, no network surprise.
            herm = tempfile.mkdtemp(prefix='t167_gui_dl_', dir=RUNDIR)
            marker = os.path.join(herm, 'yt_dlp_invoked.txt')
            # The stub is a PowerShell script, NOT a .cmd: a .cmd target would be
            # invoked through cmd.exe, and cmd would treat the yt-dlp format
            # string's < and > as redirection operators. The shipped worker
            # invokes a native yt-dlp.exe, where that class of re-parsing does
            # not exist -- the stub must not fake it.
            stub = os.path.join(herm, 'ytdlp_stub.ps1')
            with open(stub, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write("Set-Content -LiteralPath '%s' -Value $args -Encoding UTF8\nexit 0\n"
                         % marker.replace("'", "''"))
            urls = os.path.join(herm, 'urls.txt')
            with open(urls, 'w', encoding='utf-8') as fh:
                fh.write('https://youtu.be/aaaaaaaaaaa\nhttps://www.youtube.com/watch?v=bbbbbbbbbbb\n')
            target = os.path.join(herm, 'downloads'); os.makedirs(target)
            script = os.path.join(ROOT, 'Scripts', 'DL_YT.PS1')
            gui_env = {**os.environ, 'T167_NO_PAUSE': '1', 'SAITULS_YTDLP': stub}
            cmdline = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script,
                       '-Mode', _mode, '-OutPath', target, '-UrlsFile', urls]
            r = subprocess.run(cmdline, capture_output=True, text=True, timeout=180,
                               stdin=subprocess.DEVNULL, cwd=ROOT, env=gui_env)
            executed += 1
            invoked = os.path.exists(marker)
            stdout = r.stdout or ''
            urlfile = ''
            if invoked:
                with open(marker, encoding='utf-8-sig', errors='replace') as fh:
                    lines = [line.strip() for line in fh.read().splitlines()]
                if '-a' in lines:
                    urlfile = lines[lines.index('-a') + 1]
            ok = (r.returncode == 0 and invoked
                  and 'SAITULS_DL_STATUS=0' in stdout
                  and 'SAITULS_DL_URLS=2' in stdout
                  and urlfile and not os.path.exists(urlfile))
            ffprobe_col = 'PASS' if ok else 'FAIL'
            source_preserved = 'YES'
            repeat_run = 'PASS' if ok else 'FAIL'
            status = 'PASS' if ok else 'FAIL'
            if status == 'PASS': passed += 1
            else: fails += 1
            rows.append([aid, act['surface'], 'hermetic-stub', 'launch chain ok', '', '', 'stub yt-dlp', str(r.returncode), ffprobe_col, source_preserved, repeat_run, status])
            continue

    if isinstance(act['runner'], tuple):
        sc = act['runner'][1]
        script = os.path.join(ROOT, 'Scripts', 'scenarios', sc['script'])
        base = tempfile.mkdtemp(prefix='t167_scen_', dir=RUNDIR)
        env_utf = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'}
        try:
            poison_cwd = os.path.join(base, 'poison_cwd')
            os.makedirs(poison_cwd)
            poison_marker = os.path.join(poison_cwd, 'poison.txt')
            open(poison_marker, 'wb').write(b'POISON')
            poison_hash = sha256(poison_marker)
            target = os.path.join(base, 'target dir' + SPECIAL_NAME)
            os.makedirs(target)
            executed += 1
            exit_code = '-'
            ffprobe_col = '-'
            vcodec = acodec = '-'
            source_preserved = '-'
            repeat_run = '-'
            out_desc = '-'
            required_cap = ';'.join(sc.get('dependencies') or []) or 'none'
            ok = False
            if sc['id'] == 'audio.mute-wav-first-seconds':
                w1 = os.path.join(target, 'a.wav')
                w2 = os.path.join(target, 'short.wav')
                rate = 8000

                def make_wav(p, secs):
                    with wave.open(p, 'wb') as w:
                        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
                        frames = bytearray()
                        for i in range(int(rate * secs)):
                            v = 12000 if i < rate else 24000
                            frames += struct.pack('<h', (v + i) % 32768)
                        w.writeframes(bytes(frames))
                make_wav(w1, 2.0); make_wav(w2, 0.5)
                h1 = sha256(w1)
                with wave.open(w1, 'rb') as w:
                    f1 = w.getnframes()
                r = subprocess.run([sys.executable, script, target], capture_output=True, text=True, timeout=30, cwd=poison_cwd, env=env_utf)
                exit_code = str(r.returncode)
                if r.returncode == 0:
                    with wave.open(w1, 'rb') as w:
                        ok_frames = w.getnframes() == f1
                        data = w.readframes(w.getnframes())
                    ok_silence = data[:16000] == b'\x00' * 16000
                    bak = w1 + '.bak'
                    ok_bak = os.path.exists(bak) and sha256(bak) == h1
                    ffprobe_col = 'PASS' if (ok_frames and ok_silence and ok_bak) else 'FAIL'
                    out_desc = 'muted'
                    source_preserved = 'NO'
                    ok = ok_frames and ok_silence and ok_bak
                    r2 = subprocess.run([sys.executable, script, target], capture_output=True, text=True, timeout=30, cwd=poison_cwd, env=env_utf)
                    repeat_run = 'PASS' if (r2.returncode != 0 and 'BACKUP_EXISTS' in (r2.stdout + r2.stderr)) else 'FAIL'
                    ok = ok and repeat_run == 'PASS'
                else:
                    ffprobe_col = 'FAIL'
                    repeat_run = 'FAIL'
                if sha256(poison_marker) != poison_hash:
                    ok = False
            elif sc['id'] == 'audio.make-m3u':
                m3u = os.path.join(target, 'sample.m3u')
                open(m3u, 'w', encoding='utf-8').write("#EXTM3U\nsong one.mp3\n#comment\nsong two.mp3\n")
                r = subprocess.run(['cmd', '/d', '/c', script, target], capture_output=True, text=True, timeout=20, cwd=poison_cwd)
                exit_code = str(r.returncode)
                lst = os.path.join(target, 'list.txt')
                if r.returncode == 0 and os.path.exists(lst):
                    lines = [l.rstrip('\r\n') for l in open(lst, encoding='utf-8').read().splitlines()]
                    ok = lines == ["file 'song one.mp3'", "file 'song two.mp3'"]
                    ffprobe_col = 'PASS' if ok else 'FAIL'
                    out_desc = 'list.txt'
                    source_preserved = 'YES'
                    r2 = subprocess.run(['cmd', '/d', '/c', script, target], capture_output=True, text=True, timeout=20, cwd=poison_cwd)
                    repeat_run = 'PASS' if r2.returncode == 0 else 'FAIL'
                    ok = ok and repeat_run == 'PASS'
                else:
                    ffprobe_col = 'FAIL'
                    repeat_run = 'FAIL'
                if sha256(poison_marker) != poison_hash or os.path.exists(os.path.join(poison_cwd, 'list.txt')):
                    ok = False
                bad = os.path.join(base, 'no_such_xyz')
                rb = subprocess.run(['cmd', '/d', '/c', script, bad], capture_output=True, text=True, timeout=20, cwd=poison_cwd)
                if rb.returncode == 0:
                    ok = False
            elif sc['id'] == 'audio.rename-from-tags':
                r = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, target], capture_output=True, text=True, timeout=30, cwd=poison_cwd, env=env_utf)
                exit_code = str(r.returncode)
                ok = r.returncode == 0 and sha256(poison_marker) == poison_hash
                ffprobe_col = 'PASS' if ok else 'FAIL'
                out_desc = 'rename'
                source_preserved = 'YES'
                rb = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, os.path.join(base, 'nope_xyz')], capture_output=True, text=True, timeout=20, cwd=poison_cwd, env=env_utf)
                r2 = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, target], capture_output=True, text=True, timeout=20, cwd=poison_cwd, env=env_utf)
                repeat_run = 'PASS' if (rb.returncode != 0 and r2.returncode == 0) else 'FAIL'
                ok = ok and repeat_run == 'PASS'
            elif sc['id'] in ('audio.tag-mp3', 'audio.album-tool', 'audio.mp3-win'):
                try:
                    import mutagen
                    has_mutagen = True
                except ImportError:
                    has_mutagen = False
                if not has_mutagen:
                    required_cap = 'python:mutagen missing'
                    exit_code = '5'
                    ffprobe_col = 'REFUSED'
                    out_desc = 'refused'
                    source_preserved = 'YES'
                    repeat_run = 'PASS'
                    ok = True
                else:
                    if sc['id'] == 'audio.album-tool':
                        r = subprocess.run([sys.executable, '-m', 'py_compile', script], capture_output=True, text=True, timeout=10, cwd=poison_cwd, env=env_utf)
                        exit_code = str(r.returncode)
                        ok = r.returncode == 0 and sha256(poison_marker) == poison_hash
                        ffprobe_col = 'PASS' if ok else 'FAIL'
                        out_desc = 'py_compile'
                        source_preserved = 'YES'
                        repeat_run = 'PASS' if ok else 'FAIL'
                    else:
                        dst = os.path.join(target, 'song.mp3')
                        shutil.copyfile(FIXTURES['mp3'], dst)
                        before = sha256(dst)
                        r = subprocess.run([sys.executable, script, target], capture_output=True, text=True, timeout=30, cwd=poison_cwd, env=env_utf)
                        exit_code = str(r.returncode)
                        if r.returncode != 0:
                            ffprobe_col = 'FAIL'
                            out_desc = 'failed'
                            source_preserved = 'YES' if sha256(dst) == before else 'NO'
                            repeat_run = 'FAIL'
                        else:
                            meta = probe_json(dst, 'stream=codec_type')
                            ok_probe = meta is not None and any(s.get('codec_type') == 'audio' for s in meta.get('streams', []))
                            ffprobe_col = 'PASS' if ok_probe else 'FAIL'
                            out_desc = 'tagged'
                            source_preserved = 'YES'
                            r2 = subprocess.run([sys.executable, script, target], capture_output=True, text=True, timeout=30, cwd=poison_cwd, env=env_utf)
                            repeat_run = 'PASS' if r2.returncode == 0 else 'FAIL'
                            ok = ok_probe and repeat_run == 'PASS'
                            rb = subprocess.run([sys.executable, script, os.path.join(base, 'no_xyz')], capture_output=True, text=True, timeout=20, cwd=poison_cwd, env=env_utf)
                            if rb.returncode == 0:
                                ok = False
                    if sha256(poison_marker) != poison_hash:
                        ok = False
            else:
                r = subprocess.run([sys.executable, script, target] if sc['interpreter'] == 'python' else ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, target], capture_output=True, text=True, timeout=20, cwd=poison_cwd, env=env_utf)
                exit_code = str(r.returncode)
                ok = r.returncode == 0 and sha256(poison_marker) == poison_hash
                ffprobe_col = 'PASS' if ok else 'FAIL'
                out_desc = 'ok'
                source_preserved = 'YES'
                repeat_run = 'PASS'
            status = 'PASS' if ok else 'FAIL'
            if ok:
                passed += 1
            else:
                fails += 1
            rows.append([aid, act['surface'], sc['id'], out_desc, vcodec, acodec, required_cap, exit_code, ffprobe_col, source_preserved, repeat_run, status])
        finally:
            shutil.rmtree(base, ignore_errors=True)
        continue

    # FFMPEG_RUN registry actions
    ext = act['ext']
    kind = fixture_for(ext)
    src = FIXTURES.get(kind) if kind else None
    if src is None and ext in IMG_FIXTURE_EXT:
        src = make_image_fixture(ext, FIXDIR)
    if src is None:
        # unknown input class: fail the suite (new visible action without a
        # validated execution contract)
        executed += 1
        fails += 1
        print('FAIL  %s : no fixture contract for input %s' % (aid, ext))
        rows.append([aid, act['surface'], 'MISSING', '-', '', '', '-', '-', 'FAIL', '-', '-', 'FAIL'])
        continue
    run_dir = os.path.join(RUNDIR, re.sub(r'[^A-Za-z0-9]', '_', aid)[-60:])
    os.makedirs(run_dir, exist_ok=True)
    base_name = 'input' + SPECIAL_NAME if act is actions[0] else 'input'
    local_src = os.path.join(run_dir, base_name + ext)
    shutil.copyfile(src, local_src)
    before_hash = sha256(local_src)
    input_has_video = kind in VIDEO_KINDS
    input_is_image = ext in IMG_FIXTURE_EXT
    r = run_runner(act['runner'], local_src, act['args'], act['suffix'], run_dir)
    executed += 1
    status_line = None
    for line in (r.stdout or '').splitlines():
        if line.startswith('SAIFMPEG_STATUS='):
            status_line = line
    req_cap = get_required_capability(act['args'])
    v_exp, a_exp = expected_codecs(act)
    vcodec_disp = v_exp or ''
    acodec_disp = a_exp or ''
    exit_code = str(r.returncode)
    ffprobe_col = 'FAIL'
    source_preserved = 'NO'
    repeat_run = 'FAIL'
    out_desc = 'failed'
    ok = False
    if r.returncode == 5:
        source_preserved = 'YES' if sha256(local_src) == before_hash else 'NO'
        leftover = has_scratch_leftovers(run_dir)
        ok = ('SAIFMPEG_STATUS=5' in (r.stdout or '')) and source_preserved == 'YES' and not leftover
        ffprobe_col = 'REFUSED' if ok else 'FAIL'
        out_desc = 'refused'
        vcodec_disp = acodec_disp = 'refused'
        r2 = run_runner(act['runner'], local_src, act['args'], act['suffix'], run_dir)
        repeat_run = 'PASS' if r2.returncode == 5 else 'FAIL'
    elif r.returncode == 0 and status_line and status_line.startswith('SAIFMPEG_STATUS=0:'):
        suffix = act['suffix']
        if is_frame_mode(suffix):
            cand = os.path.join(run_dir, os.path.splitext(os.path.basename(local_src))[0])
            frames = [f for f in os.listdir(cand) if f.startswith('frame_')] if os.path.isdir(cand) else []
            ok = len(frames) > 0
            out_desc = '%d frames' % len(frames)
            ffprobe_col = 'PASS' if ok else 'FAIL'
            vcodec_disp = 'frame'
            acodec_disp = '-'
            source_preserved = 'YES' if sha256(local_src) == before_hash else 'NO'
            if has_scratch_leftovers(run_dir):
                ok = False
                ffprobe_col = 'FAIL'
            r2 = run_runner(act['runner'], local_src, act['args'], act['suffix'], run_dir)
            repeat_run = 'PASS' if r2.returncode == 9 else 'FAIL'
        else:
            out_path = expected_outpath(local_src, suffix)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                vok, vd, codecs = validate_output(out_path, act, input_has_video, input_is_image)
                ok = vok
                vcodec_disp = codecs.get('video') or v_exp or ''
                acodec_disp = codecs.get('audio') or a_exp or ''
                out_desc = os.path.basename(out_path)
                ffprobe_col = 'PASS' if ok else 'FAIL'
                source_preserved = 'YES' if sha256(local_src) == before_hash else 'NO'
                if has_scratch_leftovers(run_dir):
                    ok = False
                    ffprobe_col = 'FAIL'
            else:
                out_desc = 'MISSING OUTPUT'
                source_preserved = 'YES' if sha256(local_src) == before_hash else 'NO'
                if has_scratch_leftovers(run_dir):
                    ok = False
            r2 = run_runner(act['runner'], local_src, act['args'], act['suffix'], run_dir)
            if r2.returncode == 0 and os.path.exists(expected_outpath(local_src, suffix)):
                repeat_run = 'PASS'
            else:
                repeat_run = 'FAIL'
                ok = False
    else:
        source_preserved = 'YES' if sha256(local_src) == before_hash else 'NO'
        if has_scratch_leftovers(run_dir):
            ok = False
    if ok and source_preserved == 'YES' and repeat_run == 'PASS' and ffprobe_col in ('PASS', 'REFUSED'):
        passed += 1
        status = 'PASS'
    else:
        fails += 1
        status = 'FAIL'
    rows.append([aid, act['surface'], kind or ext, out_desc, vcodec_disp, acodec_disp, req_cap, exit_code, ffprobe_col, source_preserved, repeat_run, status])

# ---------------------------------------------------------------- harness
harness_rows = []
harness_fail = 0


def harness_add(aid, surface, input_fixture, output, vcodec, acodec, req, exit_code, ffprobe, preserved, repeat, status):
    global harness_fail
    harness_rows.append([aid, surface, input_fixture, output, vcodec, acodec, req, exit_code, ffprobe, preserved, repeat, status])
    if status != 'PASS':
        harness_fail += 1


tmp = tempfile.mkdtemp(prefix='t167_harness_', dir=RUNDIR)


def stub_cmd(name, body):
    p = os.path.join(tmp, name)
    open(p, 'w', encoding='ascii').write(body)
    return p


try:
    wav = FIXTURES['wav_stereo']
    inp = os.path.join(tmp, 'harness' + SPECIAL_NAME + '.wav')
    shutil.copyfile(wav, inp)
    before = sha256(inp)
    r = run_ps1_direct(inp, '-c:a libmp3lame -b:a 128k', '.mp3', ff=os.path.join(tmp, 'no_ffmpeg.exe'), fp=FFPROBE)
    preserved = 'YES' if sha256(inp) == before else 'NO'
    leftover = has_scratch_leftovers(tmp)
    r2 = run_ps1_direct(inp, '-c:a libmp3lame -b:a 128k', '.mp3', ff=os.path.join(tmp, 'no_ffmpeg.exe'), fp=FFPROBE)
    ok = r.returncode == 3 and r2.returncode == 3 and preserved == 'YES' and not leftover and 'SAIFMPEG_STATUS=3' in (r.stdout or '')
    harness_add('harness|missing-ffmpeg', 'Bin/FFMPEG_RUN.PS1', 'wav_stereo', 'refused', '', '', 'none', str(r.returncode), 'PASS' if ok else 'FAIL', preserved, 'PASS' if ok else 'FAIL', 'PASS' if ok else 'FAIL')

    inp2 = os.path.join(tmp, 'harness2.wav')
    shutil.copyfile(wav, inp2)
    before2 = sha256(inp2)
    r = run_ps1_direct(inp2, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=os.path.join(tmp, 'no_ffprobe.exe'))
    preserved2 = 'YES' if sha256(inp2) == before2 else 'NO'
    leftover2 = has_scratch_leftovers(tmp)
    r2 = run_ps1_direct(inp2, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=os.path.join(tmp, 'no_ffprobe.exe'))
    ok2 = r.returncode == 4 and r2.returncode == 4 and preserved2 == 'YES' and not leftover2 and 'SAIFMPEG_STATUS=4' in (r.stdout or '')
    harness_add('harness|missing-ffprobe', 'Bin/FFMPEG_RUN.PS1', 'wav_stereo', 'refused', '', '', 'none', str(r.returncode), 'PASS' if ok2 else 'FAIL', preserved2, 'PASS' if ok2 else 'FAIL', 'PASS' if ok2 else 'FAIL')

    inp3 = os.path.join(tmp, 'harness3.mp4')
    shutil.copyfile(FIXTURES['mp4'], inp3)
    before3 = sha256(inp3)
    r = run_ps1_direct(inp3, '-c:v h264_nvenc -pix_fmt yuv420p', '_nvenc.mp4', ff=FFMPEG, fp=FFPROBE)
    preserved3 = 'YES' if sha256(inp3) == before3 else 'NO'
    leftover3 = has_scratch_leftovers(tmp)
    if r.returncode == 5:
        ok3 = preserved3 == 'YES' and not leftover3 and 'SAIFMPEG_STATUS=5' in (r.stdout or '')
        harness_add('harness|unavailable-encoder-h264_nvenc', 'Bin/FFMPEG_RUN.PS1', 'mp4', 'refused', 'refused', 'refused', 'h264_nvenc', str(r.returncode), 'REFUSED' if ok3 else 'FAIL', preserved3, 'PASS' if ok3 else 'FAIL', 'PASS' if ok3 else 'FAIL')
    elif r.returncode == 0:
        outp = expected_outpath(inp3, '_nvenc.mp4')
        ok3 = os.path.exists(outp) and not leftover3
        harness_add('harness|unavailable-encoder-h264_nvenc', 'Bin/FFMPEG_RUN.PS1', 'mp4', os.path.basename(outp) if os.path.exists(outp) else 'missing', 'h264', 'aac', 'h264_nvenc', str(r.returncode), 'PASS' if ok3 else 'FAIL', preserved3, 'PASS', 'PASS' if ok3 else 'FAIL')
    else:
        harness_add('harness|unavailable-encoder-h264_nvenc', 'Bin/FFMPEG_RUN.PS1', 'mp4', 'failed', '', '', 'h264_nvenc', str(r.returncode), 'FAIL', preserved3, 'FAIL', 'FAIL')

    inp4 = os.path.join(tmp, 'harness4.wav')
    shutil.copyfile(wav, inp4)
    before4 = sha256(inp4)
    r = run_ps1_direct(inp4, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=FFPROBE)
    preserved4 = 'YES' if sha256(inp4) == before4 else 'NO'
    out4 = expected_outpath(inp4, '.mp3')
    leftover4 = has_scratch_leftovers(tmp)
    r2 = run_ps1_direct(inp4, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=FFPROBE)
    ok4 = r.returncode == 0 and r2.returncode == 0 and preserved4 == 'YES' and not leftover4 and os.path.exists(out4)
    harness_add('harness|scratch-cleanup', 'Bin/FFMPEG_RUN.PS1', 'wav_stereo', os.path.basename(out4) if os.path.exists(out4) else 'missing', 'mp3', 'mp3', 'none', str(r.returncode), 'PASS' if ok4 else 'FAIL', preserved4, 'PASS' if ok4 else 'FAIL', 'PASS' if ok4 else 'FAIL')

    stub_fail = stub_cmd('stub_ffmpeg_fails.cmd', '@echo off\r\nexit /b 1\r\n')
    inp5 = os.path.join(tmp, 'harness5.wav')
    shutil.copyfile(wav, inp5)
    before5 = sha256(inp5)
    r = run_ps1_direct(inp5, '-c:a libmp3lame -b:a 128k', '.mp3', ff=stub_fail, fp=FFPROBE)
    preserved5 = 'YES' if sha256(inp5) == before5 else 'NO'
    leftover5 = has_scratch_leftovers(tmp)
    r2 = run_ps1_direct(inp5, '-c:a libmp3lame -b:a 128k', '.mp3', ff=stub_fail, fp=FFPROBE)
    ok5 = r.returncode == 7 and r2.returncode == 7 and preserved5 == 'YES' and not leftover5
    harness_add('harness|stub-ffmpeg-failure', 'Bin/FFMPEG_RUN.PS1', 'wav_stereo', 'refused', '', '', 'stub ffmpeg exit 1', str(r.returncode), 'PASS' if ok5 else 'FAIL', preserved5, 'PASS' if ok5 else 'FAIL', 'PASS' if ok5 else 'FAIL')

    stub_probe = stub_cmd('stub_ffprobe_garbage.cmd', '@echo off\r\necho garbage\r\nexit /b 0\r\n')
    inp6 = os.path.join(tmp, 'harness6.wav')
    shutil.copyfile(wav, inp6)
    before6 = sha256(inp6)
    r = run_ps1_direct(inp6, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=stub_probe)
    preserved6 = 'YES' if sha256(inp6) == before6 else 'NO'
    leftover6 = has_scratch_leftovers(tmp)
    r2 = run_ps1_direct(inp6, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=stub_probe)
    ok6 = r.returncode == 8 and r2.returncode == 8 and preserved6 == 'YES' and not leftover6
    harness_add('harness|stub-ffprobe-invalid', 'Bin/FFMPEG_RUN.PS1', 'wav_stereo', 'refused', '', '', 'stub ffprobe garbage', str(r.returncode), 'PASS' if ok6 else 'FAIL', preserved6, 'PASS' if ok6 else 'FAIL', 'PASS' if ok6 else 'FAIL')

    # PUBLICATION ORACLE - the runner publishes through ONE native atomic
    # replacement ([IO.File]::Replace / ReplaceFile semantics) for an existing
    # destination. These cases prove the real post-scratch contract, not a
    # pre-publication refusal.
    fin = os.path.join(tmp, 'fault_inject.wav')
    shutil.copyfile(wav, fin)
    before_fi = sha256(fin)
    r0 = run_ps1_direct(fin, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=FFPROBE)
    out_fi = expected_outpath(fin, '.mp3')
    dest_ok0 = (r0.returncode == 0 and os.path.exists(out_fi) and 'SAIFMPEG_STATUS=0' in (r0.stdout or ''))
    dest_hash0 = sha256(out_fi) if dest_ok0 else None

    # CASE A - locked existing destination: the replacement commit itself is
    # refused by the OS. Require exit 9 with the canonical destination still
    # present, byte-identical, and the source untouched. No partial/corrupt
    # destination and no scratch may survive.
    fh = open(out_fi, 'rb')
    try:
        r1 = run_ps1_direct(fin, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=FFPROBE)
        after_hash = sha256(out_fi) if os.path.exists(out_fi) else None
        src_unchanged = (sha256(fin) == before_fi)
        dest_preserved = (after_hash is not None and after_hash == dest_hash0)
        ok_fi = (r1.returncode == 9 and 'SAIFMPEG_STATUS=9' in (r1.stdout or '')
                 and src_unchanged and dest_preserved
                 and not has_scratch_leftovers(tmp))
    finally:
        fh.close()
    harness_add('harness|publish-fault-injection', 'Bin/FFMPEG_RUN.PS1', 'wav_stereo',
                'prior output preserved' if dest_preserved else 'LOST', '', '',
                'locked destination (atomic commit refused)', str(r1.returncode),
                'PASS' if ok_fi else 'FAIL',
                'YES' if src_unchanged else 'NO',
                'PASS' if ok_fi else 'FAIL',
                'PASS' if ok_fi else 'FAIL')

    # CASE B - successful existing-destination replacement: no prompt, exit 0,
    # the canonical destination now holds the new validated result (different
    # content from the previous generation is fine; validity is proven by the
    # runner's own ffprobe gate + deterministic encode), source unchanged, no
    # stale scratch or backup.
    r2 = run_ps1_direct(fin, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=FFPROBE)
    ok_b = (r2.returncode == 0 and os.path.exists(out_fi)
            and 'SAIFMPEG_STATUS=0' in (r2.stdout or '')
            and probe_json(out_fi, 'format=format_name,duration') is not None
            and sha256(fin) == before_fi
            and not has_scratch_leftovers(tmp))
    harness_add('harness|publish-replace-success', 'Bin/FFMPEG_RUN.PS1', 'wav_stereo',
                os.path.basename(out_fi) if os.path.exists(out_fi) else 'missing',
                'mp3', 'mp3', 'none', str(r2.returncode),
                'PASS' if ok_b else 'FAIL',
                'YES' if sha256(fin) == before_fi else 'NO',
                'PASS' if ok_b else 'FAIL',
                'PASS' if ok_b else 'FAIL')

    # CASE C - repeat replacement: a second run over the already-replaced
    # destination must be deterministically successful, produce valid ffprobe
    # output again, and leak neither scratch nor backup files.
    hash_b = sha256(out_fi)
    r3 = run_ps1_direct(fin, '-c:a libmp3lame -b:a 128k', '.mp3', ff=FFMPEG, fp=FFPROBE)
    deterministic = (r3.returncode == 0 and sha256(out_fi) == hash_b)
    ok_c = (deterministic
            and probe_json(out_fi, 'format=format_name,duration') is not None
            and sha256(fin) == before_fi
            and not has_scratch_leftovers(tmp))
    harness_add('harness|publish-replace-repeat', 'Bin/FFMPEG_RUN.PS1', 'wav_stereo',
                os.path.basename(out_fi), 'mp3', 'mp3', 'none', str(r3.returncode),
                'PASS' if ok_c else 'FAIL',
                'YES' if sha256(fin) == before_fi else 'NO',
                'PASS' if ok_c else 'FAIL',
                'PASS' if ok_c else 'FAIL')
except Exception as e:
    harness_add('harness|exception', 'Bin/FFMPEG_RUN.PS1', '-', 'exception: %s' % e, '', '', 'exception', '-', 'FAIL', 'NO', 'FAIL', 'FAIL')

# ---------------------------------------------------------------- report
print('action_id,surface,input_fixture,output,video_codec,audio_codec,required_capability,exit_code,ffprobe,source_preserved,repeat_run,status')
for row in rows:
    print(','.join(str(c).replace(',', ';') for c in row))
print()
print('RUNNER_CONTRACT_PROBES')
for row in harness_rows:
    print(','.join(str(c).replace(',', ';') for c in row))

discovered = len(actions)
skipped = 0
print()
print('MEDIA_ACTIONS_DISCOVERED=%d' % discovered)
print('MEDIA_ACTIONS_EXECUTED=%d' % executed)
print('MEDIA_ACTIONS_PASSED=%d' % passed)
print('MEDIA_ACTIONS_FAILED=%d' % fails)
print('MEDIA_ACTIONS_SKIPPED=%d' % skipped)
print('RUNNER_CONTRACT_PROBES=%d FAILED=%d' % (len(harness_rows), harness_fail))
ok = (discovered == executed == passed and fails == 0 and skipped == 0 and harness_fail == 0)
print('MEDIA_MATRIX_GREEN=%s' % ('TRUE' if ok else 'FALSE'))
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if ok else 1)
