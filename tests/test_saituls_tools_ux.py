# -*- coding: utf-8 -*-
# T-164 / T-167: SAITULS -> Tools is a normal, honest tool surface.
#
# Contract proof for the shell itself (the behavioural halves are proven by
# test_clipboard_plus_control.py, test_download_yt.py, test_media_conversions.py
# and test_merge_aud_topology.py):
#   * every visible media/clipboard action points at a worker that EXISTS;
#   * MEDIA readiness is measured (real --version probes), never "the file
#     exists";
#   * the download source (the clipboard) is inspected before launch, with an
#     actionable refusal, and the worker refuses again;
#   * Merge Audio preflights FFMPEG.EXE *and* FFPROBE.EXE and reports the
#     worker's reason in the shell instead of a transient console;
#   * Clipboard+ start/stop/safe-copy/restore/settings are real actions:
#     pythonw, named-mutex idempotence, an explicit stop channel, the status
#     file -- and no kill of a healthy resident anywhere;
#   * every first-party Registry verb points at a script that exists;
#   * SAITULS.cs still compiles with the documented csc command.
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
SAITULS = os.path.join(ROOT, 'SAITULS.cs')
CSC = r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'

# Payload-provided executables are not part of a source checkout; the media
# matrix (tests/test_media_conversions.py) proves they exist and run once the
# payload is installed.
PAYLOAD_BINARIES = {
    'FFMPEG.EXE', 'FFPROBE.EXE', 'yt-dlp.exe', 'deno.exe', 'ARIA2C.EXE',
    'LAUNCHER.EXE', '__CONTEXTMENU+.EXE', 'AV1_COMPRESS.BAT',
}

fails = 0
passed = 0


def check(name, ok, detail=''):
    global fails, passed
    print(('PASS  ' if ok else 'FAIL  ') + name + ('  ' + str(detail) if detail else ''))
    if ok:
        passed += 1
    else:
        fails += 1


def read(path):
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def section(text, start_marker, end_marker):
    start = text.find(start_marker)
    if start < 0:
        return ''
    end = text.find(end_marker, start)
    return text[start:end if end > 0 else len(text)]


def grid_rows(text, name):
    match = re.search(r'string\[,\]\s+%s\s*=\s*\{(.*?)\};' % name, text, re.S)
    if not match:
        return []
    return re.findall(r'\{\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\}', match.group(1))


def kills_are_probe_timeouts(src):
    """For every p.Kill() in the shell, prove it is the timeout branch of a
    bounded WaitForExit(ms) probe child -- never a resident-management kill.
    Returns a list of booleans aligned with the Kill sites."""
    verdicts = []
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if '.Kill(' not in line:
            continue
        before = '\n'.join(lines[max(0, i - 8):i])
        verdicts.append('WaitForExit(' in before and 'timeoutMs)' in before
                        and 'if (!exited)' in before.replace('!p.WaitForExit', 'if (!exited)'))
    return verdicts


def main():
    src = read(SAITULS)
    tools = section(src, 'void DrawToolsTab(', 'int DrawJobLine(')
    clipboard_helper = section(src, 'static class ClipboardPlusHelper', 'static class ClipboardInput')
    download_dialog = section(src, 'class DownloadDialog : Form', 'class SaitulsForm : Form')

    # ------------------------------------------------------------- MEDIA ------
    media_rows = grid_rows(src, 'MediaDefs')
    check('MEDIA section declares the three documented actions',
          sorted([r[0] for r in media_rows]) == ['Download Audio', 'Download Video', 'Merge Audio'],
          media_rows)
    missing_workers = [r[1] for r in media_rows if not os.path.exists(os.path.join(ROOT, 'Scripts', r[1]))]
    check('every MEDIA row points at a worker that exists on disk', not missing_workers, missing_workers)
    check('the MEDIA section draws from that same grid (no second list to drift)',
          'MediaDefs.GetLength(0)' in tools)
    check('MEDIA shows one compact readiness line',
          'Media tools: ' in src and '_lastProbe' in tools)
    check('readiness runs the tools instead of trusting filenames',
          "Toolchain.Probe(Path.Combine(bin, \"FFMPEG.EXE\"), \"-version\"" in src
          and "\"FFPROBE.EXE\"), \"-version\"" in src
          and "\"yt-dlp.exe\"), \"--version\"" in src
          and 'JavaScript runtime' in src)
    check('readiness is re-measurable and install/repair invalidates the cache',
          'ResetMediaProbe' in src and 'ResetMediaProbe();' in section(src, 'void StartSetup()', 'void OpenPath('))
    check('the download worker is the ONLY download implementation (thin CMD)',
          'DL_YT.PS1' in read(os.path.join(ROOT, 'Scripts', 'DL_YT.CMD'))
          and 'yt-dlp.exe" -a' not in read(os.path.join(ROOT, 'Scripts', 'DL_YT.CMD')))

    # ---------------------------------------------------------- downloads -----
    check('the clipboard is inspected as the download input',
          'ClipboardInput.ExtractUrls' in src and 'ContainsFileDropList' in src
          and 'No downloadable URL found in the clipboard' in src)
    check('the clipboard is inspected again at launch, not only in the dialog',
          src.count('ClipboardInput.ExtractUrls') >= 2)
    check('the downloader options dialog exposes mode, playlist/date and the folder',
          'class DownloadDialog : Form' in src and '"Playlist"' in download_dialog
          and '"Prefix upload date"' in download_dialog and 'FolderBrowserDialog' in download_dialog
          and '_folder.Text' in download_dialog)
    check('a download job keeps running/completed/failed and the destination',
          'DrawJobLine' in src and 'java' not in src
          and 'RUNNING' in src and 'COMPLETED' in src and '"FAILED (exit "' in src)
    check('a failed job reports the worker reason, not a vanished console',
          'SAITULS_DL_ERROR=' in src and 'ReportJobFailure' in src
          and 'ReportJobFailure(job, title)' in section(src, 'void CaptureJobOutput(', 'void ReportJobFailure('))
    check('a failed job can be inspected: the log is kept and openable',
          'saituls-download-' in src and 'Show log' in src)

    # --------------------------------------------------------- merge audio ----
    merge_blocker = section(src, 'string MergeBlocker()', 'void PickAndMergeAudio(')
    check('Merge Audio preflights FFMPEG.EXE AND FFPROBE.EXE',
          '"FFMPEG.EXE"' in merge_blocker and '"FFPROBE.EXE"' in merge_blocker
          and 'FFprobe is missing' in merge_blocker)
    check('the merge file picker filters media files',
          'of.Filter' in src and '*.mkv' in src)
    check('the merge result is reported in the shell (success and refusal)',
          'CaptureJobOutput(p, job, "Merge Audio", true)' in src
          and 'ReportJobSuccess' in src and 'ReportJobFailure' in src)
    check('the merge worker runs hidden with its output captured',
          'psi.EnvironmentVariables["MERGE_IN"]' in src and 'RedirectStandardOutput = true' in src)

    # --------------------------------------------------------- clipboard+ -----
    check('Clipboard+ start launches pythonw (no console flash)',
          'pythonw.exe' in clipboard_helper and '--start' in clipboard_helper
          and 'CreateNoWindow = true' in clipboard_helper)
    check('Clipboard+ ownership is the named mutex',
          'Local\\SaitulsClipboardPlus' in clipboard_helper
          and 'Mutex.OpenExisting' in clipboard_helper)
    check('Clipboard+ stop is an explicit named stop channel',
          'Local\\SaitulsClipboardPlusStop' in clipboard_helper
          and 'EventWaitHandle.OpenExisting' in clipboard_helper)
    check('Clipboard+ helper never kills a resident by name (probes only, on timeout)',
          'GetProcessesByName' not in src and 'taskkill' not in src.lower()
          and all(kills_are_probe_timeouts(src)),
          [i for i in kills_are_probe_timeouts(src) if not i])
    check('Clipboard+ dependency readiness is measured by the subsystem',
          '--check-deps' in clipboard_helper and 'DEPENDENCY MISSING' in src)
    check('Clipboard+ Safe Copy / Restore / Settings are real actions',
          '--sanitize-text-once' in clipboard_helper and '--restore-last-safe-copy' in clipboard_helper
          and '--settings' in clipboard_helper)
    check('Clipboard+ status surfaces save folder, hotkey, monitor and usability',
          '"save_path_ok"' in src and '"effective_save_path"' in src
          and '"hotkey_registered"' in src and 'SAVE PATH UNAVAILABLE' in src)
    check('Secret hygiene: the shell never stores or prints clipboard contents',
          'sanitized' not in tools.lower().replace('safe copy', '')
          and 'GLOBAL_HISTORY' not in src)
    check('every Clipboard+ button has a real handler',
          all(name in src for name in ('ClipboardStartAction', 'ClipboardStopAction',
                                       'ClipboardSafeCopyAction', 'ClipboardRestoreAction',
                                       'ClipboardSettingsAction')))

    # ------------------------------------------------------- no dead buttons --
    tool_rows = grid_rows(src, 'ToolDefs')
    unknown = []
    for label, script, mode in tool_rows:
        if mode in ('none', 'folder', 'file'):
            continue
        if mode not in ('saipatch', 'saipatch-settings', 'saipatch-queue-viewer',
                        'scenarios', 'secureapps', 'consoles', 'shelldoctor', 'itemlist', 'itemtree',
                        'saispin-settings'):
            unknown.append((label, mode))
    check('every OTHER TOOLS row uses a mode the shell handles', not unknown, unknown)
    dead = []
    for label, script, mode in tool_rows:
        if mode in ('saipatch', 'saipatch-settings', 'saipatch-queue-viewer',
                    'scenarios', 'secureapps', 'consoles', 'shelldoctor', 'saispin-settings', 'none'):
            continue
        if not os.path.exists(os.path.join(ROOT, 'Scripts', script)):
            dead.append((label, script))
    check('no enabled tool button targets a missing backend', not dead, dead)
    check('Shell Doctor button launches the shipped subsystem script',
          os.path.exists(os.path.join(ROOT, 'Scripts', 'shell_doctor', 'SHELL_DOCTOR.ps1'))
          and '"shell_doctor", "SHELL_DOCTOR.ps1"' in src)
    check('SAISPIN Settings has a non-elevated Tools launcher',
          os.path.exists(os.path.join(ROOT, 'Scripts', 'saispin_settings.ps1'))
          and '"SAISPIN Settings", "saispin_settings.ps1", "saispin-settings"' in src
          and 'if (mode == "saispin-settings")' in section(src, 'void LaunchTool(', '// SAIPATCH:'))

    # ------------------------------------------------- Registry verb closure --
    reg_dir = os.path.join(ROOT, 'Registry')
    reg_missing = []
    for name in sorted(os.listdir(reg_dir)):
        if not name.upper().endswith('.REG') or name.upper().endswith('_REM.REG'):
            continue
        for match in re.finditer(r'%%ROOT%%\\\\\\\\([^"\\]+(?:\\\\[^"\\]+)*)', read(os.path.join(reg_dir, name))):
            rel = match.group(1).replace('\\\\', os.sep)
            if not rel:
                continue
            full = os.path.join(ROOT, rel)
            head, tail = os.path.split(rel)
            if head.lower() == 'bin' and tail in PAYLOAD_BINARIES:
                continue
            if not os.path.exists(full):
                reg_missing.append('%s -> %s' % (name, rel))
    check('every first-party Registry action points at a file that exists',
          not reg_missing, reg_missing[:4])

    # ------------------------------------------------------------ compiles ---
    if os.path.exists(CSC):
        out = os.path.join(os.environ.get('TEMP', ROOT), 'saituls_ux_check.exe')
        result = subprocess.run([CSC, '-nologo', '-target:winexe', '-out:' + out, '-optimize+',
                                 '-r:System.dll', '-r:System.Drawing.dll', '-r:System.Windows.Forms.dll',
                                 'SAITULS.cs'],
                                cwd=ROOT, capture_output=True, text=True, timeout=300)
        detail = (result.stdout + result.stderr).strip()[:300]
        check('SAITULS.cs compiles with the documented csc command', result.returncode == 0, detail)
        try:
            if os.path.exists(out):
                os.remove(out)
        except Exception:
            pass
    else:
        check('SAITULS.cs compiles with the documented csc command', False,
              'csc.exe not found at ' + CSC)

    print()
    print('SAITULS_TOOLS_UX: passed=%d failed=%d' % (passed, fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
