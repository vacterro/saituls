import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import winreg

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
REG = os.path.join(ROOT, 'Registry')
FFMPEG_MENU = os.path.join(REG, 'FFMPEG_MENU.REG')
FFMPEG_MENU_REM = os.path.join(REG, 'FFMPEG_MENU_REM.REG')
MKV_FIX = os.path.join(REG, 'MKV_FIX.REG')

REAL_BASE = r'SOFTWARE\Classes\SystemFileAssociations'
TEST_EXTS = ['.wav', '.mp3', '.mkv', '.jpg', '.webm']
ACCESS_READ = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
ACCESS_WRITE = winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY

fails = 0
passed = 0


def check(name, ok, detail=''):
    global fails, passed
    print(('PASS  ' if ok else 'FAIL  ') + name + ('  ' + detail if detail else ''))
    if ok:
        passed += 1
    else:
        fails += 1


def reg(*args):
    return subprocess.run(['reg'] + list(args), capture_output=True, text=True)


def key_exists(path):
    return reg('query', path).returncode == 0


def read_reg(path):
    raw = open(path, 'rb').read()
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    return raw.decode('utf-8-sig')


def make_copy(src, dst, hive_name):
    body = read_reg(src)
    body = body.replace('HKEY_CURRENT_USER', hive_name)
    body = body.replace('%%ROOT%%', ROOT.replace('\\', '\\\\'))
    with open(dst, 'wb') as stream:
        stream.write(body.encode('utf-16'))


def snapshot_key(relative_path):
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, relative_path, 0, ACCESS_READ)
    except FileNotFoundError:
        return None
    with key:
        values = []
        index = 0
        while True:
            try:
                values.append(winreg.EnumValue(key, index))
                index += 1
            except OSError:
                break
        children = []
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
                children.append((name, snapshot_key(relative_path + '\\' + name)))
                index += 1
            except OSError:
                break
    values.sort(key=lambda value: value[0].casefold())
    children.sort(key=lambda child: child[0].casefold())
    return tuple(values), tuple(children)


def restore_key(relative_path, state):
    reg('delete', 'HKCU\\' + relative_path, '/f')
    if state is None:
        return

    def create(path, node):
        values, children = node
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, ACCESS_WRITE) as key:
            for name, data, value_type in values:
                winreg.SetValueEx(key, name, 0, value_type, data)
        for name, child in children:
            create(path + '\\' + name, child)

    create(relative_path, state)


def affected_extensions():
    pattern = re.compile(
        r'^\[-?HKEY_CURRENT_USER\\SOFTWARE\\Classes\\SystemFileAssociations\\([^\\\]]+)',
        re.MULTILINE | re.IGNORECASE,
    )
    extensions = set()
    for path in (FFMPEG_MENU, FFMPEG_MENU_REM, MKV_FIX):
        extensions.update(pattern.findall(read_reg(path)))
    if not extensions or not set(TEST_EXTS).issubset(extensions):
        raise RuntimeError('cannot determine complete registry mutation scope')
    return sorted(extensions, key=str.casefold)


def try_loaded_hive(work):
    token = uuid.uuid4().hex
    seed = r'HKCU\Software\T167HiveSeed_' + token
    hive_file = os.path.join(work, 'isolated.dat')
    mount = 'T167_' + token
    try:
        if reg('add', seed, '/f').returncode != 0:
            return None
        saved = reg('save', seed, hive_file, '/y')
        if saved.returncode != 0:
            return None
    finally:
        reg('delete', seed, '/f')
    loaded = reg('load', 'HKU\\' + mount, hive_file)
    return mount if loaded.returncode == 0 else None


extensions = affected_extensions()
try:
    original = {ext: snapshot_key(REAL_BASE + '\\' + ext) for ext in extensions}
except Exception as exc:
    print('REGISTRY_SENTINEL: snapshot failed closed:', exc)
    sys.exit(2)

work = tempfile.mkdtemp(prefix='t167_reg_')
mount = None
fallback = False
cleanup_errors = []
try:
    mount = try_loaded_hive(work)
    fallback = mount is None
    hive_name = 'HKEY_CURRENT_USER' if fallback else 'HKEY_USERS\\' + mount
    base = ('HKCU' if fallback else 'HKU\\' + mount) + r'\SOFTWARE\Classes\SystemFileAssociations'
    print('registry isolation:', 'snapshot/restore fallback' if fallback else 'temporary HKU hive')

    tmp_mkv = os.path.join(work, 'MKV_FIX.token.reg')
    tmp_menu = os.path.join(work, 'FFMPEG_MENU.token.reg')
    tmp_remove = os.path.join(work, 'FFMPEG_MENU_REM.token.reg')
    make_copy(MKV_FIX, tmp_mkv, hive_name)
    make_copy(FFMPEG_MENU, tmp_menu, hive_name)
    make_copy(FFMPEG_MENU_REM, tmp_remove, hive_name)

    for ext in TEST_EXTS:
        path = base + '\\' + ext + r'\shell\ThirdPartyPlay\command'
        result = reg('add', path, '/ve', '/d', r'"C:\Windows\notepad.exe" "%1"', '/f')
        check('third-party seeded ' + ext, result.returncode == 0, result.stderr.strip()[:100])

    result = reg('import', tmp_mkv)
    check('MKV_FIX import', result.returncode == 0, result.stderr.strip()[:100])
    result = reg('import', tmp_menu)
    check('FFMPEG_MENU import', result.returncode == 0, result.stderr.strip()[:100])
    result = reg('import', tmp_menu)
    check('repeat install ok', result.returncode == 0, result.stderr.strip()[:100])

    check('menu installed: .wav Convert root', key_exists(base + r'\.wav\shell\Конвертировать'))
    check('menu installed: .mkv Options root', key_exists(base + r'\.mkv\shell\Опции'))
    check('mkv_fix installed: Сжать в MP4', key_exists(base + r'\.mkv\shell\Опции\shell\Сжать в MP4'))
    check('mkv_fix installed: SVT-AV1', key_exists(base + r'\.mkv\shell\Опции\shell\Сжать в MP4\shell\SVT-AV1'))

    result = reg('import', tmp_remove)
    check('FFMPEG_MENU_REM import', result.returncode == 0, result.stderr.strip()[:100])

    check('menu gone: .wav Convert root', not key_exists(base + r'\.wav\shell\Конвертировать'))
    check('menu gone: .mp4 Convert root', not key_exists(base + r'\.mp4\shell\Конвертировать'))
    check('menu gone: .mkv Convert root', not key_exists(base + r'\.mkv\shell\Конвертировать'))
    check('menu gone: .mkv own Options children',
          not key_exists(base + r'\.mkv\shell\Опции\shell\Извлечь Аудио'))
    check('menu gone: .jpg Convert root', not key_exists(base + r'\.jpg\shell\Конвертировать'))

    for ext in TEST_EXTS:
        check('third-party survived ' + ext,
              key_exists(base + '\\' + ext + r'\shell\ThirdPartyPlay\command'))
    check('MKV_FIX survived menu uninstall',
          key_exists(base + r'\.mkv\shell\Опции\shell\Сжать в MP4'))
    check('MKV_FIX SVT-AV1 survived',
          key_exists(base + r'\.mkv\shell\Опции\shell\Сжать в MP4\shell\SVT-AV1'))

    result = reg('import', tmp_remove)
    check('repeat uninstall ok', result.returncode == 0, result.stderr.strip()[:100])
    check('survivors still there after repeat',
          key_exists(base + r'\.mkv\shell\Опции\shell\Сжать в MP4'))
except Exception as exc:
    check('test execution', False, repr(exc))
finally:
    if fallback:
        for ext in extensions:
            try:
                restore_key(REAL_BASE + '\\' + ext, original[ext])
            except Exception as exc:
                cleanup_errors.append(ext + ': ' + repr(exc))
        check('snapshot restore completed', not cleanup_errors, '; '.join(cleanup_errors)[:300])
    elif mount:
        result = reg('unload', 'HKU\\' + mount)
        check('temporary hive unloaded', result.returncode == 0, result.stderr.strip()[:100])
    try:
        restored = {ext: snapshot_key(REAL_BASE + '\\' + ext) for ext in extensions}
        check('real HKCU byte-semantic equivalence', restored == original)
    except Exception as exc:
        check('real HKCU equivalence proof', False, repr(exc))
    try:
        shutil.rmtree(work)
    except Exception as exc:
        check('temporary files removed', False, repr(exc))

print()
print('REGISTRY_SENTINEL: passed=%d failed=%d' % (passed, fails))
sys.exit(1 if fails else 0)
