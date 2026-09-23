# -*- coding: utf-8 -*-
# T-164: Clipboard+ as a normal, observable, configurable SAITULS tool.
#
# Deterministic contract proof for the subsystem the shell drives:
#   * one settings file (configurable save folder, hotkey, explicit fallback);
#   * a Windows named mutex decides ownership -- start is idempotent, a second
#     start never kills or replaces a healthy resident and never erases its
#     RAM-only Safe Copy history, stop is explicit, restart after stop is fresh;
#   * an unavailable save folder is a reported STATE, not a crash: it is never
#     silently redirected, images are not silently dropped, Unicode works;
#   * dependency readiness is measured (real imports), so a resident that would
#     die on import is never started;
#   * the image monitor keeps the accepted behaviour: PNG, GIF, Explorer file
#     copies untouched, own clipboard rewrite ignored, bounded dedup state.
#
# Two suites touch the real Windows clipboard (the operator-history and the
# hotkey-registration cases). Both save and restore the previous clipboard text
# and unregister anything they registered.
import ctypes
import importlib.util
import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
CLIPBOARD_PLUS = os.path.join(ROOT, 'Scripts', 'saipatch', 'clipboard+.pyw')

spec = importlib.util.spec_from_file_location('clipboard_plus_control', CLIPBOARD_PLUS)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

HOTKEY_MOD = cp.win32con.MOD_CONTROL | cp.win32con.MOD_ALT | cp.win32con.MOD_SHIFT
HOTKEY_VK = ord('C')

fails = 0
passed = 0


def check(name, ok, detail=''):
    global fails, passed
    print(('PASS  ' if ok else 'FAIL  ') + name + ('  ' + str(detail) if detail else ''))
    if ok:
        passed += 1
    else:
        fails += 1


def write_settings(state_dir, save_path, hotkey='Ctrl+Alt+Shift+C',
                   fallback_enabled=False, fallback_path=''):
    path = os.path.join(state_dir, 'clipboard-plus.ini')
    text = ('[clipboard+]\r\n'
            'save_path=%s\r\n'
            'hotkey=%s\r\n'
            'fallback_enabled=%s\r\n'
            'fallback_path=%s\r\n'
            % (save_path, hotkey, '1' if fallback_enabled else '0', fallback_path))
    with open(path, 'w', encoding='utf-16', newline='') as fh:
        fh.write(text)
    return path


def make_png():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new('RGBA', (4, 4), (255, 0, 0, 128)).save(buffer, format='PNG')
    return buffer.getvalue()


def make_gif():
    from PIL import Image
    buffer = io.BytesIO()
    frames = [Image.new('P', (4, 4), 1), Image.new('P', (4, 4), 2)]
    frames[0].save(buffer, format='GIF', save_all=True, append_images=frames[1:], duration=100, loop=0)
    return buffer.getvalue()


# ==============================================================================
# 1. Settings, hotkey grammar, bounded dedup -- pure, no processes
# ==============================================================================

def test_settings_roundtrip():
    work = tempfile.mkdtemp(prefix='cbp_settings_')
    unicode_dir = os.path.join(work, 'тест пробел 印')
    os.makedirs(unicode_dir)
    path = os.path.join(work, 'clipboard-plus.ini')
    values = {'save_path': unicode_dir, 'hotkey': 'Ctrl+Shift+F5',
              'fallback_enabled': True, 'fallback_path': os.path.join(work, 'fb')}
    cp.save_settings(values, path)
    loaded = cp.load_settings(path)
    check('settings: the Unicode save folder survives a write/read round trip',
          loaded['save_path'] == os.path.abspath(unicode_dir), loaded['save_path'])
    check('settings: hotkey and fallback flags persist',
          loaded['hotkey'] == 'Ctrl+Shift+F5' and loaded['fallback_enabled'] is True
          and loaded['fallback_path'] == os.path.abspath(os.path.join(work, 'fb')))
    raw = open(path, 'rb').read()
    check('settings: the file is UTF-16 (GetPrivateProfileStringW reads it)',
          raw[:2] == b'\xff\xfe', raw[:2])
    cp.reset_settings(path)
    reset = cp.load_settings(path)
    check('settings: reset returns the documented defaults',
          reset['save_path'] == cp.DEFAULT_SAVE_PATH
          and reset['hotkey'] == cp.DEFAULT_HOTKEY
          and reset['fallback_enabled'] is False)
    check('settings: an absent file reads as defaults, never as a crash',
          cp.load_settings(os.path.join(work, 'nope.ini'))['save_path'] == cp.DEFAULT_SAVE_PATH)


def test_hotkey_grammar():
    cases = {'Ctrl+Alt+Shift+C': True, 'ctrl+alt+c': True, 'Ctrl+Shift+F5': True,
             'Win+Alt+M': True, 'Ctrl+Alt+Shift+ZZ': False, 'Ctrl': False,
             'Alt+F13': True, 'F5': False, 'Ctrl++': False, 'Ctrl+Alt+Shift': False}
    for spec_text, valid in cases.items():
        ok = True
        reason = ''
        try:
            modifiers, vk = cp.parse_hotkey(spec_text)
            ok = (modifiers != 0 and vk > 0)
        except ValueError as exc:
            ok = False
            reason = str(exc)
        if valid:
            check('hotkey grammar accepts %r' % spec_text, ok, reason)
        else:
            check('hotkey grammar refuses %r with a reason' % spec_text, not ok and reason != '',
                  reason if reason else 'accepted')
    modifiers, vk = cp.parse_hotkey('Ctrl+Alt+Shift+F5')
    check('hotkey grammar formats what it parsed',
          cp.format_hotkey(modifiers, vk) == 'Ctrl+Alt+Shift+F5', cp.format_hotkey(modifiers, vk))


def test_bounded_dedup():
    bounded = cp.BoundedHashSet(3)
    for i in range(5):
        bounded.add('hash%d' % i)
    check('bounded dedup: the collection cannot grow past its capacity',
          len(bounded) == 3, len(bounded))
    check('bounded dedup: the newest hashes stay authoritative',
          'hash4' in bounded and 'hash3' in bounded and 'hash0' not in bounded)


# ==============================================================================
# 2. Save folder usability and image behaviour -- in-process, real files
# ==============================================================================

class MonitorBehaviourTests(unittest.TestCase):

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix='cbp_monitor_')
        self.status = cp.RuntimeStatus(path=os.path.join(self.work, 'status.ini'))

    def monitor(self, save_path, **kwargs):
        return cp.LightweightClipboardMonitor(save_path=save_path, status=self.status, **kwargs)

    def test_missing_drive_is_a_state_not_a_crash(self):
        missing = 'Q:\\definitely_not_here\\_Clipboard_stuff'
        monitor = self.monitor(missing)
        self.assertFalse(monitor.save_path_ok)
        self.assertEqual(monitor.effective_save_path, '')
        self.assertIn('save_path_ok=0', open(os.path.join(self.work, 'status.ini'), encoding='utf-16').read())

        data = make_png()
        monitor.get_clipboard_sequence = lambda: 7
        monitor.get_clipboard_image = lambda: (data, 'PNG')
        with patch.object(monitor, 'set_image_to_clipboard', return_value=True):
            processed = monitor.process_clipboard()
        self.assertFalse(processed)
        # The image is NOT silently discarded: it is held (one bounded slot) for
        # a retry, and the reason is in the status file for the shell to show.
        self.assertIsNotNone(monitor.unsaved_pending)
        text = open(os.path.join(self.work, 'status.ini'), encoding='utf-16').read()
        self.assertIn('unsaved_pending=1', text)

    def test_fallback_is_used_only_when_enabled_and_the_effective_path_is_shown(self):
        fallback = os.path.join(self.work, 'фолбэк 印')
        monitor = self.monitor('Q:\\definitely_not_here\\_Clipboard_stuff',
                               fallback_enabled=False, fallback_path=fallback)
        self.assertFalse(monitor.save_path_ok)
        self.assertEqual(monitor.effective_save_path, '')

        monitor = self.monitor('Q:\\definitely_not_here\\_Clipboard_stuff',
                               fallback_enabled=True, fallback_path=fallback)
        self.assertTrue(monitor.save_path_ok)
        self.assertTrue(monitor.fallback_active)
        self.assertEqual(monitor.effective_save_path, os.path.abspath(fallback))
        text = open(os.path.join(self.work, 'status.ini'), encoding='utf-16').read()
        self.assertIn('fallback_active=1', text)

    def test_unicode_save_path_png_and_gif(self):
        target = os.path.join(self.work, 'тест пробел 印')
        monitor = self.monitor(target)
        self.assertTrue(monitor.save_path_ok, monitor.save_path_error)
        rewrites = []
        for payload, fmt, extension in ((make_png(), 'PNG', '.png'), (make_gif(), 'GIF', '.gif')):
            monitor.get_clipboard_sequence = lambda: monitor.last_clipboard_sequence + 1
            monitor.get_clipboard_image = lambda p=payload, f=fmt: (p, f)
            with patch.object(monitor, 'set_image_to_clipboard',
                              side_effect=lambda *a, **k: rewrites.append(a[1]) or True):
                self.assertTrue(monitor.process_clipboard())
        files = sorted(os.listdir(target))
        self.assertEqual(len(files), 2, files)
        self.assertTrue(any(f.endswith('.png') for f in files), files)
        self.assertTrue(any(f.endswith('.gif') for f in files), files)
        self.assertTrue(all(os.path.getsize(os.path.join(target, f)) > 0 for f in files))
        self.assertEqual(len(rewrites), 2)
        self.assertLessEqual(len(monitor.processed_hashes), cp.MAX_DEDUP_HASHES)

    def test_own_rewrite_is_ignored_and_text_is_never_sanitized_automatically(self):
        monitor = self.monitor(self.work)
        sequence = {'value': 10}
        monitor.get_clipboard_sequence = lambda: sequence['value']
        monitor.get_clipboard_image = lambda: (None, None)
        monitor.process_clipboard()
        self.assertEqual(monitor.last_clipboard_sequence, 10)
        # A text clipboard (no image) never reaches the sanitizer on its own.
        sequence['value'] = 11
        with patch.object(cp, 'safe_copy_clipboard') as sanitizer:
            self.assertFalse(monitor.process_clipboard())
            sanitizer.assert_not_called()
        # The rewrite this monitor performed advances the sequence it tracks, so
        # its own change is not processed again.
        self.assertEqual(monitor.last_clipboard_sequence, 11)

    def test_save_failure_keeps_the_monitor_alive_and_retries_later(self):
        monitor = self.monitor(self.work)
        data = make_png()
        monitor.get_clipboard_sequence = lambda: 3
        monitor.get_clipboard_image = lambda: (data, 'PNG')
        real_save = monitor.save_image_file
        monitor.save_image_file = lambda d, f: (None, None)
        with patch.object(monitor, 'set_image_to_clipboard', return_value=True):
            self.assertFalse(monitor.process_clipboard())
        self.assertIsNotNone(monitor.unsaved_pending)
        # The drive comes back: the held image is written, without rewriting the
        # clipboard the user may already be using for something newer.
        monitor.save_image_file = real_save
        monitor._last_path_check = 0.0
        monitor._retry_pending_save()
        self.assertIsNone(monitor.unsaved_pending)
        self.assertEqual(len([f for f in os.listdir(self.work) if f.endswith('.png')]), 1)

    def test_apply_settings_switches_the_folder_and_reports_it(self):
        first = os.path.join(self.work, 'one')
        second = os.path.join(self.work, 'two 印')
        monitor = self.monitor(first)
        monitor.apply_settings({'save_path': second, 'fallback_enabled': False})
        self.assertEqual(monitor.save_path, os.path.abspath(second))
        self.assertEqual(monitor.effective_save_path, os.path.abspath(second))
        self.assertTrue(os.path.isdir(second))


# ==============================================================================
# 3. Real resident lifecycle: mutex ownership, idempotent start, explicit stop
# ==============================================================================

def clipboard_get_text():
    import win32clipboard
    if win32clipboard.IsClipboardFormatAvailable(cp.win32con.CF_HDROP):
        return None
    if not win32clipboard.IsClipboardFormatAvailable(cp.win32con.CF_UNICODETEXT):
        return None
    opened = False
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            opened = True
            break
        except Exception:
            time.sleep(0.05)
    if not opened:
        return None
    try:
        return win32clipboard.GetClipboardData(cp.win32con.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def clipboard_set_text(text):
    import win32clipboard
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(cp.win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            time.sleep(0.05)
    return False


class ResidentLifecycleTests(unittest.TestCase):
    """Real resident processes, isolated by their own mutex/state directory."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix='cbp_state_')
        self.save_dir = tempfile.mkdtemp(prefix='cbp_save_')
        token = uuid.uuid4().hex
        self.env = dict(os.environ)
        self.env['SAITULS_CLIPBOARD_PLUS_STATE_DIR'] = self.state
        self.env['SAITULS_CLIPBOARD_PLUS_MUTEX'] = 'Local\\SaitulsClipboardPlusTest-' + token
        self.env['SAITULS_CLIPBOARD_PLUS_STOP_EVENT'] = 'Local\\SaitulsClipboardPlusTestStop-' + token
        # A private loopback port: the control channel is not the ownership
        # primitive, and another Clipboard+ (for example the operator's own
        # resident) may legitimately be listening on the default one.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(('127.0.0.1', 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.env['SAITULS_CLIPBOARD_PLUS_PORT'] = str(self.port)
        self.env['PYTHONIOENCODING'] = 'utf-8'
        self.settings = write_settings(self.state, self.save_dir)
        self.resident = None
        self.registered_hotkey = False

    def tearDown(self):
        if self.resident is not None and self.resident.poll() is None:
            self.run_cp(['--stop'], timeout=30)
            try:
                self.resident.wait(timeout=15)
            except Exception:
                try:
                    self.resident.kill()
                except Exception:
                    pass
        if self.registered_hotkey:
            ctypes.windll.user32.UnregisterHotKey(None, 4242)
            self.registered_hotkey = False

    # -- helpers ------------------------------------------------------------
    def run_cp(self, args, timeout=60):
        return subprocess.run([sys.executable, CLIPBOARD_PLUS, *args],
                              capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, cwd=ROOT, env=self.env)

    def spawn_resident(self, extra=None):
        log_path = os.path.join(self.state, 'resident.log')
        log = open(log_path, 'ab')
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        self.resident = subprocess.Popen([sys.executable, CLIPBOARD_PLUS, '--start', '--no-hotkey',
                                          *(extra or [])],
                                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         cwd=ROOT, env=self.env, creationflags=flags)
        return self.resident

    def wait_for_status(self, running, timeout=25.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.run_cp(['--status'], timeout=30)
            if (result.returncode == 0) == running:
                return True
            time.sleep(0.5)
        return False

    def status_field(self, key):
        for line in open(os.path.join(self.state, 'status.ini'), encoding='utf-16').read().splitlines():
            if line.startswith(key + '='):
                return line.split('=', 1)[1].strip()
        return ''

    def test_01_status_before_start(self):
        result = self.run_cp(['--status'], timeout=30)
        check('residency: --status reports STOPPED with a non-zero exit before start',
              result.returncode == 3 and 'STOPPED' in result.stdout, result.stdout.strip())

    def test_02_start_is_idempotent_and_preserves_ram_history(self):
        self.spawn_resident()
        check('residency: the first start reaches RUNNING', self.wait_for_status(True),
              open(os.path.join(self.state, 'resident.log')).read()[-300:])
        first_pid = self.status_field('pid')
        check('residency: the resident reports itself in its status file', first_pid != '', first_pid)
        check('residency: the monitor and the configured folder are reported',
              self.status_field('monitor') == '1' and self.status_field('save_path_ok') == '1'
              and self.status_field('effective_save_path') == os.path.abspath(self.save_dir),
              self.status_field('effective_save_path'))
        check('residency: the control channel reports itself ready',
              self.status_field('control_channel') == 'ok',
              self.status_field('control_error'))

        second = self.run_cp(['--start', '--no-hotkey'], timeout=60)
        check('residency: a second start returns ALREADY_RUNNING and exits 0',
              second.returncode == 0 and 'ALREADY_RUNNING' in second.stdout, second.stdout.strip())
        check('residency: the second start did not replace the resident',
              self.status_field('pid') == first_pid, self.status_field('pid'))

        # RAM-only Safe Copy history: the resident must still own it after the
        # second start. (This touches the real clipboard; the operator text is
        # restored below.)
        original = clipboard_get_text()
        if original is None:
            self.skipTest('Live clipboard contains non-text data; preserve it. '
                          'Run this integration case with text on the clipboard.')
        try:
            tracking = 'https://example.com/page?utm_source=chat&gclid=abc123&id=42'
            self.assertTrue(clipboard_set_text(tracking))
            safe = self.run_cp(['--sanitize-text-once'], timeout=60)
            sanitized = clipboard_get_text() or ''
            check('residency: Safe Copy runs through the resident and sanitizes',
                  'utm_source' not in sanitized and 'gclid' not in sanitized,
                  safe.stdout.strip() + ' | ' + sanitized)
            self.run_cp(['--start', '--no-hotkey'], timeout=60)
            restored = self.run_cp(['--restore-last-safe-copy'], timeout=60)
            check('residency: Restore Original still has the RAM copy after a second start',
                  'Restored' in restored.stdout, restored.stdout.strip())
            check('residency: the original text came back byte for byte',
                  clipboard_get_text() == tracking, repr(clipboard_get_text())[:120])
        finally:
            clipboard_set_text(original)

    def test_03_stop_then_restart(self):
        self.spawn_resident()
        self.assertTrue(self.wait_for_status(True))
        first_pid = self.status_field('pid')

        stopped = self.run_cp(['--stop'], timeout=60)
        check('residency: --stop asks for a clean exit and reports STOPPED',
              stopped.returncode == 0 and 'STOPPED' in stopped.stdout, stopped.stdout.strip())
        check('residency: the resident process really ended', self.wait_for_status(False))
        try:
            self.resident.wait(timeout=15)
        except Exception:
            pass
        check('residency: the resident exited by itself (no kill)', self.resident.poll() is not None,
              self.resident.poll())
        check('residency: the status file records the clean stop',
              self.status_field('stopped') == '1' and self.status_field('monitor') == '0')

        self.spawn_resident()
        check('residency: start after stop brings a fresh resident up', self.wait_for_status(True))
        check('residency: the restart is a new process, not a resurrected one',
              self.status_field('pid') not in ('', first_pid), self.status_field('pid'))

    def test_04_hotkey_registration_failure_is_visible(self):
        # Occupy the configured hotkey from this thread: the resident's own
        # registration must then fail and SAY SO, while the image monitor keeps
        # running. Nothing here is simulated.
        import win32con
        user32 = ctypes.windll.user32
        occupied = user32.RegisterHotKey(None, 4242,
                                         win32con.MOD_CONTROL | win32con.MOD_ALT | win32con.MOD_SHIFT,
                                         ord('C'))
        if not occupied:
            check('hotkey failure: the test could occupy Ctrl+Alt+Shift+C', False,
                  'RegisterHotKey refused in the test process')
            return
        self.registered_hotkey = True
        try:
            log = open(os.path.join(self.state, 'resident.log'), 'ab')
            flags = 0x00000008 | 0x00000200
            self.resident = subprocess.Popen([sys.executable, CLIPBOARD_PLUS, '--start'],
                                             stdout=log, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, cwd=ROOT, env=self.env,
                                             creationflags=flags)
            self.assertTrue(self.wait_for_status(True))
            check('hotkey failure: the resident reports hotkey_registered=0',
                  self.status_field('hotkey_registered') == '0',
                  self.status_field('hotkey_error'))
            check('hotkey failure: the reason is recorded, not swallowed',
                  'RegisterHotKey' in self.status_field('hotkey_error'),
                  self.status_field('hotkey_error'))
            check('hotkey failure: the image monitor still runs',
                  self.status_field('monitor') == '1'
                  and self.status_field('save_path_ok') == '1')
        finally:
            user32.UnregisterHotKey(None, 4242)
            self.registered_hotkey = False

    def test_05_dependency_readiness(self):
        ready = self.run_cp(['--check-deps'], timeout=90)
        check('readiness: --check-deps proves pywin32 + Pillow with real calls',
              ready.returncode == 0 and 'DEPENDENCY_STATUS=READY' in ready.stdout,
              ready.stdout.strip())
        # -S removes site-packages: the real missing-dependency path, not a flag
        # that pretends dependencies are gone.
        missing = subprocess.run([sys.executable, '-S', CLIPBOARD_PLUS, '--check-deps'],
                                 capture_output=True, text=True, timeout=90,
                                 stdin=subprocess.DEVNULL, cwd=ROOT, env=self.env)
        check('readiness: without site-packages the report is MISSING with exit 4',
              missing.returncode == 4 and 'DEPENDENCY_STATUS=MISSING' in missing.stdout
              and 'MISSING=' in missing.stdout, missing.stdout.strip()[:200])

    def test_06_resident_refuses_to_start_without_dependencies(self):
        state = tempfile.mkdtemp(prefix='cbp_nodeps_')
        env = dict(self.env)
        env['SAITULS_CLIPBOARD_PLUS_STATE_DIR'] = state
        write_settings(state, self.save_dir)
        result = subprocess.run([sys.executable, '-S', CLIPBOARD_PLUS, '--start'],
                                capture_output=True, text=True, timeout=90,
                                stdin=subprocess.DEVNULL, cwd=ROOT, env=env)
        check('readiness: a resident that cannot import is never started',
              result.returncode == 4 and 'DEPENDENCY MISSING' in result.stdout,
              result.stdout.strip()[:200])


def main():
    print('--- settings / grammar / bounded state ---')
    test_settings_roundtrip()
    test_hotkey_grammar()
    test_bounded_dedup()

    print('\n--- monitor behaviour (in-process, real files) ---')
    suite = unittest.TestLoader().loadTestsFromTestCase(MonitorBehaviourTests)
    result = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
    global fails, passed
    passed += result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)
    fails += len(result.failures) + len(result.errors)

    print('\n--- resident lifecycle (real processes, isolated mutex) ---')
    suite = unittest.TestLoader().loadTestsFromTestCase(ResidentLifecycleTests)
    result = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite)
    passed += result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)
    fails += len(result.failures) + len(result.errors)

    print()
    print('CLIPBOARD_PLUS_CONTROL: passed=%d failed=%d' % (passed, fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
