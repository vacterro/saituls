# -*- coding: utf-8 -*-
# T-167 / T-164: the download path is validated BEFORE yt-dlp is started.
#
# Deterministic proof for Scripts\DL_YT.PS1 (the single implementation) and its
# thin Scripts\DL_YT.CMD launcher:
#   * empty, prose, file-copy and non-http clipboard input is refused with an
#     actionable reason (exit 3) and yt-dlp is never invoked;
#   * one URL and multi-line URL input are accepted;
#   * audio/video/playlist/dated modes produce the documented arguments;
#   * Unicode destinations and destinations with spaces survive quoting;
#   * every run owns a unique URL list that is always cleaned up, so two
#     concurrent runs cannot consume each other's links;
#   * the child exit code is propagated.
#
# yt-dlp itself is replaced by a PowerShell stub through the worker's documented
# SAITULS_YTDLP override: no network, no real download. A .ps1 stub (not .cmd)
# is required on purpose -- cmd.exe would re-parse the video format string's
# < and > as redirection, which the shipped native yt-dlp.exe never sees.
import os
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
WORKER = os.path.join(ROOT, 'Scripts', 'DL_YT.PS1')
CMD_LAUNCHER = os.path.join(ROOT, 'Scripts', 'DL_YT.CMD')
BUNDLED_FFMPEG = os.path.join(ROOT, 'Bin', 'FFMPEG.EXE')

fails = 0
passed = 0


def check(name, ok, detail=''):
    global fails, passed
    print(('PASS  ' if ok else 'FAIL  ') + name + ('  ' + str(detail) if detail else ''))
    if ok:
        passed += 1
    else:
        fails += 1


def ffmpeg_override():
    # The worker only requires the ffmpeg path to exist; CI has no payload.
    return BUNDLED_FFMPEG if os.path.exists(BUNDLED_FFMPEG) else sys.executable


def make_stub(folder, name, exit_code=0, marker=None):
    """A yt-dlp stand-in that records each argument on its own line."""
    stub = os.path.join(folder, name + '.ps1')
    lines = []
    if marker:
        lines.append("Set-Content -LiteralPath '%s' -Value $args -Encoding UTF8"
                     % marker.replace("'", "''"))
    lines.append('exit %d' % exit_code)
    with open(stub, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines) + '\n')
    return stub


def read_args(marker):
    with open(marker, encoding='utf-8-sig', errors='replace') as fh:
        return [line.strip() for line in fh.read().splitlines()]


def run_worker(args, env=None, timeout=180):
    base = dict(os.environ)
    base['T167_NO_PAUSE'] = '1'
    if env:
        base.update(env)
    return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                           '-File', WORKER, *args],
                          capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, cwd=ROOT, env=base)


def temp_files():
    temp = os.environ.get('TEMP') or tempfile.gettempdir()
    try:
        return [name for name in os.listdir(temp) if name.startswith('saituls-urls-')]
    except Exception:
        return []


def set_clipboard_text(text):
    import win32clipboard
    win32con = __import__('win32con')
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            time.sleep(0.05)
    return False


def set_clipboard_files(paths):
    import struct
    import win32clipboard
    win32con = __import__('win32con')
    payload = b''
    for path in paths:
        payload += os.path.abspath(path).encode('utf-16le') + b'\x00\x00'
    payload += b'\x00\x00'
    data = struct.pack('IIIII', 20, 0, 0, 0, 1) + payload
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            time.sleep(0.05)
    return False


def get_clipboard_text():
    import win32clipboard
    win32con = __import__('win32con')
    if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
        return None
    if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
        return None
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)
    return None


def main():
    work = tempfile.mkdtemp(prefix='dlyt_test_')
    out = os.path.join(work, 'downloads')
    os.makedirs(out)

    # ---------------------------------------------------------------- input ---
    empty = os.path.join(work, 'empty.txt')
    open(empty, 'w', encoding='utf-8').close()
    result = run_worker(['-Mode', 'audio', '-OutPath', out, '-UrlsFile', empty,
                         '-YtDlp', make_stub(work, 'stub_empty'), '-FFmpeg', ffmpeg_override()])
    check('empty link input is refused with an actionable reason (exit 3)',
          result.returncode == 3 and 'No downloadable URL found' in result.stdout,
          (result.returncode, result.stdout.strip()[:160]))

    prose = os.path.join(work, 'prose.txt')
    with open(prose, 'w', encoding='utf-8') as fh:
        fh.write('hey, look at this video later\nno links here at all\n')
    marker = os.path.join(work, 'invoked_prose.txt')
    result = run_worker(['-Mode', 'audio', '-OutPath', out, '-UrlsFile', prose,
                         '-YtDlp', make_stub(work, 'stub_prose', marker=marker), '-FFmpeg', ffmpeg_override()])
    check('prose without a link is refused and yt-dlp is never invoked',
          result.returncode == 3 and not os.path.exists(marker), result.stdout.strip()[:160])

    scheme = os.path.join(work, 'scheme.txt')
    with open(scheme, 'w', encoding='utf-8') as fh:
        fh.write('ftp://example.com/video.mp4\n')
    result = run_worker(['-Mode', 'video', '-OutPath', out, '-UrlsFile', scheme,
                         '-YtDlp', make_stub(work, 'stub_scheme'), '-FFmpeg', ffmpeg_override()])
    check('a non-http scheme is refused as malformed input',
          result.returncode == 3 and 'Unsupported link scheme' in result.stdout,
          result.stdout.strip()[:160])

    single = os.path.join(work, 'single.txt')
    with open(single, 'w', encoding='utf-8') as fh:
        fh.write('https://youtu.be/aaaaaaaaaaa\n')
    marker = os.path.join(work, 'invoked_single.txt')
    result = run_worker(['-Mode', 'audio', '-OutPath', out, '-UrlsFile', single,
                         '-YtDlp', make_stub(work, 'stub_single', marker=marker), '-FFmpeg', ffmpeg_override()])
    check('one URL is accepted and yt-dlp is invoked',
          result.returncode == 0 and os.path.exists(marker) and 'SAITULS_DL_URLS=1' in result.stdout,
          result.stdout.strip()[:160])

    many = os.path.join(work, 'many.txt')
    with open(many, 'w', encoding='utf-8') as fh:
        fh.write('https://youtu.be/aaaaaaaaaaa\n'
                 'https://www.youtube.com/watch?v=bbbbbbbbbbb\n'
                 'watch this one: https://youtu.be/ccccccccccc\n'
                 'a note that is not a link\n')
    marker = os.path.join(work, 'invoked_many.txt')
    result = run_worker(['-Mode', 'audio', '-OutPath', out, '-UrlsFile', many,
                         '-YtDlp', make_stub(work, 'stub_many', marker=marker), '-FFmpeg', ffmpeg_override()])
    check('multi-line input is accepted (three links found)',
          result.returncode == 0 and 'SAITULS_DL_URLS=3' in result.stdout, result.stdout.strip()[:160])

    # ------------------------------------------------------- real clipboard ---
    original = get_clipboard_text()
    try:
        set_clipboard_text('just some notes copied from somewhere, no links in here')
        marker = os.path.join(work, 'invoked_clip_prose.txt')
        result = run_worker(['-Mode', 'audio', '-OutPath', out,
                             '-YtDlp', make_stub(work, 'stub_clip_prose', marker=marker),
                             '-FFmpeg', ffmpeg_override()])
        check('prose on the real clipboard is refused before any download',
              result.returncode == 3 and not os.path.exists(marker), result.stdout.strip()[:160])

        set_clipboard_files([os.path.join(work, 'not-a-url.txt')])
        result = run_worker(['-Mode', 'audio', '-OutPath', out,
                             '-YtDlp', make_stub(work, 'stub_clip_files'),
                             '-FFmpeg', ffmpeg_override()])
        check('an Explorer file copy on the clipboard is refused, not downloaded',
              result.returncode == 3 and 'copied files' in result.stdout, result.stdout.strip()[:160])

        set_clipboard_text('https://youtu.be/ddddddddddd')
        marker = os.path.join(work, 'invoked_clip_url.txt')
        result = run_worker(['-Mode', 'audio', '-OutPath', out,
                             '-YtDlp', make_stub(work, 'stub_clip_url', marker=marker),
                             '-FFmpeg', ffmpeg_override()])
        check('a URL on the real clipboard is accepted',
              result.returncode == 0 and os.path.exists(marker) and 'SAITULS_DL_URLS=1' in result.stdout,
              result.stdout.strip()[:160])
    finally:
        if original is not None:
            set_clipboard_text(original)

    # ---------------------------------------------------------------- modes ---
    marker = os.path.join(work, 'args_audio.txt')
    run_worker(['-Mode', 'audio', '-OutPath', out, '-UrlsFile', single,
                '-YtDlp', make_stub(work, 'stub_audio', marker=marker), '-FFmpeg', ffmpeg_override()])
    args = read_args(marker)
    check('audio mode asks yt-dlp for an extracted mp3',
          '--extract-audio' in args and '--audio-format' in args and 'mp3' in args
          and '--no-playlist' in args, args)
    check('audio mode passes the absolute bundled ffmpeg location',
          '--ffmpeg-location' in args and os.sep in args[args.index('--ffmpeg-location') + 1],
          args)

    marker = os.path.join(work, 'args_video.txt')
    run_worker(['-Mode', 'video', '-OutPath', out, '-UrlsFile', single,
                '-YtDlp', make_stub(work, 'stub_video', marker=marker), '-FFmpeg', ffmpeg_override()])
    args = read_args(marker)
    check('video mode merges to mkv and keeps the <=1080p selection',
          '--merge-output-format' in args and 'mkv' in args
          and any('height<=1080' in a for a in args), args)

    marker = os.path.join(work, 'args_playlist.txt')
    run_worker(['-Mode', 'video', '-Playlist', '-OutPath', out, '-UrlsFile', many,
                '-YtDlp', make_stub(work, 'stub_playlist', marker=marker), '-FFmpeg', ffmpeg_override()])
    args = read_args(marker)
    check('the playlist option is a real, exposed mode (--yes-playlist + playlist template)',
          '--yes-playlist' in args and any('playlist_index' in a for a in args), args)

    marker = os.path.join(work, 'args_dated.txt')
    run_worker(['-Mode', 'video', '-Dated', '-OutPath', out, '-UrlsFile', single,
                '-YtDlp', make_stub(work, 'stub_dated', marker=marker), '-FFmpeg', ffmpeg_override()])
    args = read_args(marker)
    check('the upload-date prefix option is a real, exposed mode',
          any('upload_date' in a for a in args), args)

    marker = os.path.join(work, 'args_legacy.txt')
    run_worker(['-Mode', 'audioplaylist', '-OutPath', out, '-UrlsFile', many,
                '-YtDlp', make_stub(work, 'stub_legacy', marker=marker), '-FFmpeg', ffmpeg_override()])
    args = read_args(marker)
    check('legacy Explorer composite modes still map onto the same worker options',
          '--yes-playlist' in args and '--extract-audio' in args, args)

    # ------------------------------------------------- unicode + quoting ------
    unicode_out = os.path.join(work, 'загрузки тест 印')
    os.makedirs(unicode_out)
    marker = os.path.join(work, 'args_unicode.txt')
    result = run_worker(['-Mode', 'audio', '-OutPath', unicode_out, '-UrlsFile', single,
                         '-YtDlp', make_stub(work, 'stub_unicode', marker=marker), '-FFmpeg', ffmpeg_override()])
    args = read_args(marker) if os.path.exists(marker) else []
    check('a Unicode destination with spaces survives quoting',
          result.returncode == 0 and any(a.startswith(unicode_out) for a in args), args[:6])

    # --------------------------------------------- concurrency isolation ------
    temp_before = set(temp_files())
    stub = make_stub(work, 'stub_concurrent')
    procs = []
    markers = []
    for index in range(2):
        marker_path = os.path.join(work, 'args_concurrent_%d.txt' % index)
        markers.append(marker_path)
        urls = os.path.join(work, 'concurrent_%d.txt' % index)
        with open(urls, 'w', encoding='utf-8') as fh:
            fh.write('https://youtu.be/concurrent%09d\n' % index)
        env = dict(os.environ)
        env['T167_NO_PAUSE'] = '1'
        env['SAITULS_YTDLP'] = stub
        # Both runs get their own marker through a tiny wrapper: the worker
        # resolves yt-dlp from SAITULS_YTDLP, so the wrapper records $args then
        # forwards nothing.
        wrapper = make_stub(work, 'wrapper_%d' % index, marker=marker_path)
        env['SAITULS_YTDLP'] = wrapper
        env['SAITULS_FFMPEG'] = ffmpeg_override()
        procs.append(subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                       '-File', WORKER, '-Mode', 'audio', '-OutPath', out,
                                       '-UrlsFile', urls],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                      stdin=subprocess.DEVNULL, cwd=ROOT, env=env))
    codes = [p.wait(timeout=240) for p in procs]
    outputs = [p.stdout.read() for p in procs if p.stdout]
    check('two concurrent downloads both succeed', codes == [0, 0], codes)
    lists = []
    for marker_path in markers:
        if os.path.exists(marker_path):
            args = read_args(marker_path)
            if '-a' in args:
                lists.append(args[args.index('-a') + 1])
    check('concurrent runs use different URL lists',
          len(lists) == 2 and lists[0] != lists[1], lists)
    check('concurrent runs leave no URL list behind',
          not [p for p in lists if os.path.exists(p)]
          and set(temp_files()) == temp_before, temp_files())

    # ------------------------------------------------------- failure paths ----
    marker = os.path.join(work, 'args_fail.txt')
    result = run_worker(['-Mode', 'video', '-OutPath', out, '-UrlsFile', single,
                         '-YtDlp', make_stub(work, 'stub_fail', exit_code=7, marker=marker),
                         '-FFmpeg', ffmpeg_override()])
    check('a failing yt-dlp propagates its exit code',
          result.returncode == 7 and 'SAITULS_DL_STATUS=7' in result.stdout,
          result.stdout.strip()[:160])
    check('a failed run still cleans its URL list',
          not [name for name in temp_files() if name not in temp_before], temp_files())
    if os.path.exists(marker):
        args = read_args(marker)
        url_list = args[args.index('-a') + 1] if '-a' in args else ''
        check('the failed run cleaned the exact URL list it used',
              url_list != '' and not os.path.exists(url_list), url_list)

    missing = run_worker(['-Mode', 'audio', '-OutPath', out, '-UrlsFile', single,
                          '-YtDlp', os.path.join(work, 'does_not_exist.exe'),
                          '-FFmpeg', ffmpeg_override()])
    check('a missing yt-dlp is refused before anything runs (exit 2)',
          missing.returncode == 2 and 'yt-dlp is missing' in missing.stdout,
          missing.stdout.strip()[:160])

    bad_mode = run_worker(['-Mode', 'flac', '-OutPath', out, '-UrlsFile', single,
                           '-YtDlp', make_stub(work, 'stub_mode'), '-FFmpeg', ffmpeg_override()])
    check('an unknown mode is refused with the accepted values',
          bad_mode.returncode == 2 and 'Unknown download mode' in bad_mode.stdout,
          bad_mode.stdout.strip()[:160])

    # ------------------------------------------------- thin CMD launcher ------
    env = dict(os.environ)
    env['T167_NO_PAUSE'] = '1'
    env['SAITULS_YTDLP'] = make_stub(work, 'stub_cmd')
    env['SAITULS_FFMPEG'] = ffmpeg_override()
    single_env = os.path.join(work, 'single_for_cmd.txt')
    with open(single_env, 'w', encoding='utf-8') as fh:
        fh.write('https://youtu.be/eeeeeeeeeee\n')
    # The launcher takes mode + destination only (the Explorer contract); the
    # link input comes from the clipboard, so point the clipboard at a URL.
    original = get_clipboard_text()
    try:
        set_clipboard_text('https://youtu.be/eeeeeeeeeee')
        cmd = subprocess.run(['cmd', '/d', '/c', CMD_LAUNCHER, 'video', out],
                             capture_output=True, text=True, timeout=240,
                             stdin=subprocess.DEVNULL, cwd=ROOT, env=env)
        check('the thin CMD launcher runs the worker and exits 0',
              cmd.returncode == 0 and 'SAITULS_DL_STATUS=0' in cmd.stdout,
              cmd.stdout.strip()[-200:])
        set_clipboard_text('nothing here but prose')
        cmd = subprocess.run(['cmd', '/d', '/c', CMD_LAUNCHER, 'audio', out],
                             capture_output=True, text=True, timeout=240,
                             stdin=subprocess.DEVNULL, cwd=ROOT, env=env)
        check('the thin CMD launcher propagates the refusal exit code',
              cmd.returncode == 3, cmd.returncode)
    finally:
        if original is not None:
            set_clipboard_text(original)

    print()
    print('DOWNLOAD_YT: passed=%d failed=%d' % (passed, fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
