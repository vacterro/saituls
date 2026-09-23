# TAG_MP3.py  v6
# Album playlist tagging engine
# Deps: pip install mutagen
# ffmpeg must be in PATH for album MP3 assembly
#
# Usage:  python TAG_MP3.py [folder]
#   If folder omitted, uses current directory.
#   Folder needs list.txt (or .m3u) + MP3 files + optional cover.png

import sys, os, re, configparser, traceback, subprocess, shutil, tempfile
from pathlib import Path

try:
    from mutagen.id3 import (
        ID3, ID3NoHeaderError,
        TIT2, TRCK, TPOS,
        TPE1, TPE2, TPE3, TPE4, TCOM, TOLY,
        TALB, TSOA, TOAL,
        TDRC, TORY,
        TCON, TMOO, TLAN, TMED,
        TCOP, TPUB, TENC, TSSE, TSRC,
        TBPM, TKEY,
        WXXX, WOAR, WOAS, WPUB,
        COMM, USLT,
        APIC,
    )
    from mutagen.mp3 import MP3
except ImportError:
    print("ОШИБКА: mutagen не установлен. Установите: pip install mutagen")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
#  1. .m3u -> list.txt
# ══════════════════════════════════════════════════════════════════════════════

def m3u_to_list_txt(folder):
    """Convert first .m3u in folder to list.txt. Returns (list_txt_path, paths)."""
    folder = Path(folder)
    m3u_files = sorted([f for f in folder.iterdir() if f.suffix.lower() == '.m3u' and f.is_file()])
    if not m3u_files:
        return None, []
    m3u_path = m3u_files[0]
    print(f"  Найден плейлист: {m3u_path.name}")
    lines_raw = []
    for enc in ('utf-8-sig', 'utf-8', 'cp1251', 'latin-1'):
        try:
            lines_raw = m3u_path.read_text(encoding=enc).splitlines()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        lines_raw = m3u_path.read_bytes().decode('latin-1').splitlines()

    full_paths = []
    for line in lines_raw:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        p = Path(line)
        if not p.is_absolute():
            p = folder / line
        full_paths.append(str(p.resolve()))

    list_txt_path = folder / 'list.txt'
    with open(list_txt_path, 'w', encoding='utf-8') as f:
        for p in full_paths:
            safe = p.replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    print(f"  list.txt обновлён из .m3u ({len(full_paths)} треков)")
    return str(list_txt_path), full_paths


# ══════════════════════════════════════════════════════════════════════════════
#  2. list.txt -> полные пути
# ══════════════════════════════════════════════════════════════════════════════

def parse_list_txt(list_path, folder):
    """Parse list.txt, return list of absolute file paths."""
    content = None
    for enc in ('utf-8-sig', 'utf-8', 'cp1251', 'latin-1'):
        try:
            with open(list_path, 'r', encoding=enc) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if content is None:
        with open(list_path, 'rb') as f:
            content = f.read().decode('latin-1')
    result = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith("file "):
            path = line[5:].strip().strip("'\"").replace("'\\''", "'")
        else:
            # Bare path line (no "file '" prefix)
            path = line.strip("'\"")
        if not os.path.isabs(path):
            path = os.path.join(folder, path)
        path = os.path.normpath(path)
        if not os.path.exists(path):
            alt = os.path.join(folder, os.path.basename(path))
            if os.path.exists(alt):
                path = alt
        result.append(path)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  3. Обложка
# ══════════════════════════════════════════════════════════════════════════════

def load_cover(folder):
    """Find and load cover artwork from folder. Returns (data, mime_type)."""
    folder = Path(folder)
    priority = [
        ('cover.png',   'image/png'),
        ('cover.jpg',   'image/jpeg'),
        ('cover.jpeg',  'image/jpeg'),
        ('cover.webp',  'image/webp'),
        ('cover.bmp',   'image/bmp'),
        ('cover.tiff',  'image/tiff'),
        ('cover.tif',   'image/tiff'),
        ('folder.jpg',  'image/jpeg'),
        ('folder.png',  'image/png'),
        ('front.jpg',   'image/jpeg'),
        ('front.png',   'image/png'),
        ('artwork.jpg', 'image/jpeg'),
        ('artwork.png', 'image/png'),
    ]
    ext_mime = {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.webp': 'image/webp', '.bmp': 'image/bmp',
        '.tiff': 'image/tiff', '.tif': 'image/tiff',
    }
    # Check priority names first
    for name, mime in priority:
        path = folder / name
        if path.is_file():
            try:
                data = path.read_bytes()
                return data, mime
            except (PermissionError, OSError) as e:
                print(f"  [img] Warning: {name} — {e}")
    # Then any suitable image file
    for f in folder.iterdir():
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in ext_mime:
            try:
                return f.read_bytes(), ext_mime[ext]
            except (PermissionError, OSError) as e:
                print(f"  [img] Warning: {f.name} — {e}")
    return None, ''


# ══════════════════════════════════════════════════════════════════════════════
#  4. Тегирование одного файла
# ══════════════════════════════════════════════════════════════════════════════

def tag_file(filepath, title, track_str, cfg, cover_data, cover_mime):
    try:
        audio = ID3(filepath)
    except ID3NoHeaderError:
        audio = ID3()

    def s(k): return cfg.get(k, '').strip()

    audio['TIT2'] = TIT2(encoding=3, text=title)
    audio['TRCK'] = TRCK(encoding=3, text=track_str)

    disc, disc_total = s('disc'), s('disc_total')
    if disc or disc_total:
        audio['TPOS'] = TPOS(encoding=3, text=f"{disc}/{disc_total}" if disc_total else disc)

    simple = {
        'artist':           ('TPE1', TPE1),
        'album_artist':     ('TPE2', TPE2),
        'conductor':        ('TPE3', TPE3),
        'remixer':          ('TPE4', TPE4),
        'composer':         ('TCOM', TCOM),
        'lyricist':         ('TOLY', TOLY),
        'album':            ('TALB', TALB),
        'album_sort':       ('TSOA', TSOA),
        'original_artist':  ('TOAL', TOAL),
        'year':             ('TDRC', TDRC),
        'date':             ('TDRC', TDRC),
        'original_year':    ('TORY', TORY),
        'genre':            ('TCON', TCON),
        'mood':             ('TMOO', TMOO),
        'language':         ('TLAN', TLAN),
        'media_type':       ('TMED', TMED),
        'copyright':        ('TCOP', TCOP),
        'publisher':        ('TPUB', TPUB),
        'encoded_by':       ('TENC', TENC),
        'encoding_tool':    ('TSSE', TSSE),
        'isrc':             ('TSRC', TSRC),
        'bpm':              ('TBPM', TBPM),
        'key':              ('TKEY', TKEY),
    }
    for key, (fid, cls) in simple.items():
        if key == 'date' and s('year'):
            continue
        val = s(key)
        if val:
            audio[fid] = cls(encoding=3, text=val)

    if s('comment'):
        audio['COMM::rus'] = COMM(encoding=3, lang='rus', desc='', text=s('comment'))
    if s('lyrics'):
        audio['USLT::rus'] = USLT(encoding=3, lang='rus', desc='', text=s('lyrics'))
    if s('url'):
        audio['WXXX:'] = WXXX(encoding=3, url=s('url'), desc='')
    if s('url_artist'):
        audio['WOAR'] = WOAR(url=s('url_artist'))
    if s('url_audio_source'):
        audio['WOAS'] = WOAS(url=s('url_audio_source'))
    if s('url_publisher'):
        audio['WPUB'] = WPUB(url=s('url_publisher'))

    # Всегда удаляем старые обложки и пишем новую
    for k in [k for k in audio.keys() if k.startswith('APIC')]:
        del audio[k]
    if cover_data:
        audio['APIC:'] = APIC(encoding=3, mime=cover_mime, type=3, desc='', data=cover_data)

    audio.save(filepath, v2_version=3, v1=2, padding=lambda x: 1024)


# ══════════════════════════════════════════════════════════════════════════════
#  5. Сбор данных треков из затегированных файлов
# ══════════════════════════════════════════════════════════════════════════════

def collect_track_data(full_paths):
    tracks = []
    for i, fp in enumerate(full_paths, start=1):
        if not os.path.exists(fp):
            continue
        try:
            audio  = MP3(fp)
            tags   = ID3(fp)
            title  = str(tags['TIT2']) if 'TIT2' in tags else os.path.splitext(os.path.basename(fp))[0]
            artist = str(tags['TPE1']) if 'TPE1' in tags else ''
            album  = str(tags['TALB']) if 'TALB' in tags else ''
            dur    = audio.info.length
        except Exception as e:
            print(f"  [WARN] collect_track_data: {os.path.basename(fp)} — {e}")
            title  = os.path.splitext(os.path.basename(fp))[0]
            artist = album = ''
            dur    = 0.0
        tracks.append({
            'track': i, 'title': title, 'artist': artist,
            'album': album, 'duration': dur, 'filepath': fp,
        })
    return tracks


# ══════════════════════════════════════════════════════════════════════════════
#  6. CUE sheet
# ══════════════════════════════════════════════════════════════════════════════

def fmt_cue(sec):
    sec = max(0.0, sec)
    m = int(sec // 60)
    s = int(sec % 60)
    f = int(round((sec - int(sec)) * 75))
    if f >= 75:
        f = 74
    return f"{m:02d}:{s:02d}:{f:02d}"


def build_cue(tracks, cfg, album_mp3_name):
    aa = (cfg.get('album_artist') or cfg.get('artist') or '').strip()
    at = cfg.get('album', '').strip()
    yr = (cfg.get('year') or cfg.get('date') or '').strip()
    ge = cfg.get('genre', '').strip()
    co = cfg.get('comment', '').strip()
    ur = cfg.get('url', '').strip()

    L = []
    if aa: L.append(f'PERFORMER "{aa}"')
    if at: L.append(f'TITLE "{at}"')
    if yr: L.append(f'REM DATE {yr}')
    if ge: L.append(f'REM GENRE {ge}')
    if co: L.append(f'REM COMMENT "{co}"')
    if ur: L.append(f'REM URL {ur}')
    L.append(f'FILE "{album_mp3_name}" MP3')

    cur = 0.0
    for t in tracks:
        ta = t['artist'] or aa
        L.append(f'  TRACK {t["track"]:02d} AUDIO')
        L.append(f'    TITLE "{t["title"]}"')
        if ta:
            L.append(f'    PERFORMER "{ta}"')
        L.append(f'    INDEX 01 {fmt_cue(cur)}')
        cur += t['duration']

    L.append(f'REM TOTALDURATION {fmt_cue(cur)}')
    return '\n'.join(L) + '\n'


# ══════════════════════════════════════════════════════════════════════════════
#  7. Обновление метаданных в существующем .cue
# ══════════════════════════════════════════════════════════════════════════════

def update_cue_metadata(cue_path, cfg):
    with open(cue_path, 'r', encoding='utf-8-sig') as f:
        lines = f.readlines()

    aa = (cfg.get('album_artist') or cfg.get('artist') or '').strip()
    at = cfg.get('album', '').strip()
    yr = (cfg.get('year') or cfg.get('date') or '').strip()
    ge = cfg.get('genre', '').strip()
    co = cfg.get('comment', '').strip()
    ur = cfg.get('url', '').strip()

    gkeys = {
        'PERFORMER':   f'PERFORMER "{aa}"'   if aa else None,
        'TITLE':       f'TITLE "{at}"'       if at else None,
        'REM DATE':    f'REM DATE {yr}'      if yr else None,
        'REM GENRE':   f'REM GENRE {ge}'     if ge else None,
        'REM COMMENT': f'REM COMMENT "{co}"' if co else None,
        'REM URL':     f'REM URL {ur}'       if ur else None,
    }

    updated  = []
    replaced = set()
    for line in lines:
        is_global = not line.startswith((' ', '\t'))
        matched = False
        if is_global:
            for key, nv in gkeys.items():
                if line.rstrip().upper().startswith(key):
                    if nv:
                        updated.append(nv + '\n')
                    replaced.add(key)
                    matched = True
                    break
        if not matched:
            updated.append(line)

    ins = [v + '\n' for k, v in gkeys.items() if k not in replaced and v]
    if ins:
        final = []
        done  = False
        for line in updated:
            if not done and line.strip().upper().startswith('FILE '):
                final.extend(ins)
                done = True
            final.append(line)
        updated = final

    with open(cue_path, 'w', encoding='utf-8-sig') as f:
        f.writelines(updated)


# ══════════════════════════════════════════════════════════════════════════════
#  8. Сборка альбомного MP3 через ffmpeg
#     ffmpeg list пишется в %TEMP% — нет кириллицы в пути
# ══════════════════════════════════════════════════════════════════════════════

def build_album_mp3(tracks, folder, album_name, cover_data, cover_mime):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        print("  ffmpeg не найден в PATH — альбомный MP3 пропущен.")
        print("  Скачайте: https://ffmpeg.org/download.html")
        return None

    safe = re.sub(r'[\\/*?:"<>|]', '_', album_name) or 'album'
    out  = os.path.join(folder, f"{safe}.mp3")

    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False) as tf:
        for t in tracks:
            path = os.path.abspath(t['filepath']).replace('\\', '/')
            tf.write(f"file '{path}'\n")
        tmp = tf.name

    print(f"  ffmpeg: сборка {safe}.mp3 ...")
    res = subprocess.run(
        [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', tmp, '-c', 'copy', out],
        capture_output=True
    )
    try:
        os.unlink(tmp)
    except OSError:
        pass

    if res.returncode != 0:
        err = (res.stderr or b'').decode('utf-8', errors='replace')
        print(f"  ffmpeg ОШИБКА:\n{err[-1200:]}")
        return None

    try:
        alb = ID3(out)
    except ID3NoHeaderError:
        alb = ID3()
    for k in [k for k in alb.keys() if k.startswith('APIC')]:
        del alb[k]
    if cover_data:
        alb['APIC:'] = APIC(encoding=3, mime=cover_mime, type=3, desc='', data=cover_data)
    alb.save(out, v2_version=3, v1=2, padding=lambda x: 1024)
    print(f"  Готово: {os.path.basename(out)}")
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  HELP
# ══════════════════════════════════════════════════════════════════════════════

def print_help():
    print("TAG_MP3.py v6 — Album playlist tagging engine")
    print()
    print("Использование: python TAG_MP3.py [options] [folder]")
    print("               python ALBUM_TOOL.py [folder]   (vintage GUI)")
    print()
    print("Опции:")
    print("  --help         Показать эту справку")
    print("  --tag-only     Только тегирование, без альбома/CUE")
    print("  --build-album  Собрать альбомный MP3 (ffmpeg, без тегов)")
    print("  --gen-cue      Создать/обновить CUE sheet (без тегов)")
    print()
    print("Процесс:")
    print("  1. Поместите MP3 + обложку в папку")
    print("  2. Создайте .m3u плейлист или list.txt с порядком треков")
    print("  3. Настройте album_tags.ini (рядом со скриптом)")
    print("  4. Запустите: python TAG_MP3.py /путь/к/папке")
    print()
    print("Результат: ID3v2 теги, обложка, альбомный MP3, CUE")
    print("Зависимости: pip install mutagen")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def resolve_target():
    """T-166 launch contract: the launcher's positional target is the authority."""
    args = sys.argv[1:]
    cli_flags = [a for a in args if a.startswith('--')]
    positional = [a for a in args if not a.startswith('--')]

    if not positional:
        return None
    t = Path(positional[0]).expanduser().resolve()
    if not t.is_dir():
        print(f"[REFUSE] target is not an existing folder: {t}", file=sys.stderr)
        sys.exit(2)
    return t


def main():
    # Parse CLI flags for GUI integration
    args = sys.argv[1:]
    cli_flags = [a for a in args if a.startswith('--')]
    positional = [a for a in args if not a.startswith('--')]

    if '--help' in cli_flags or '-h' in cli_flags or '-?' in cli_flags:
        print_help()
        return

    folder = resolve_target() or Path.cwd()
    if not folder.is_dir():
        print(f"ОШИБКА: Папка не найдена: {folder}")
        return

    tag_only = '--tag-only' in cli_flags
    build_album = '--build-album' in cli_flags
    gen_cue = '--gen-cue' in cli_flags
    skip_tagging = build_album or gen_cue  # These modes skip tagging

    print(f"Папка: {folder}\n")

    # Config and cover always loaded first
    script_dir  = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, 'album_tags.ini')
    config = configparser.ConfigParser()
    if os.path.exists(config_path):
        config.read(config_path, encoding='utf-8')
        print(f"[cfg] Конфиг загружен: {config_path}")
    else:
        print(f"[cfg] Предупреждение: album_tags.ini не найден ({config_path})")
    cfg = dict(config['tags']) if 'tags' in config else {}

    cover_data, cover_mime = load_cover(folder)
    print(f"[img] Обложка: {'найдена (' + cover_mime + ')' if cover_data else 'не найдена'}")

    # All MP3s in folder (non-recursive)
    mp3_in_folder = sorted([
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.lower().endswith('.mp3') and os.path.isfile(os.path.join(folder, f))
    ])

    # ═══════════════════════════════════════════════════
    # Single MP3 mode
    # ═══════════════════════════════════════════════════
    if len(mp3_in_folder) == 1 and not skip_tagging:
        fp    = mp3_in_folder[0]
        title = os.path.splitext(os.path.basename(fp))[0]
        print(f"\n[solo] Один MP3 — обновление тегов и обложки ...")
        try:
            tag_file(fp, title, '1', cfg, cover_data, cover_mime)
            print(f"  OK: {title}")
        except Exception:
            traceback.print_exc()
        print(f"\n{'='*50}")
        print(f"Готово: {os.path.basename(fp)}")
        return

    if len(mp3_in_folder) == 0:
        print("\nОШИБКА: MP3-файлы в папке не найдены.")
        return

    # ── Multi-track: parse playlist ────────────────────────
    if not skip_tagging:
        print(f"\n[1/3] Поиск .m3u для обновления list.txt ...")
        list_txt_path, full_paths = m3u_to_list_txt(folder)
        if not list_txt_path:
            list_txt_path = os.path.join(folder, 'list.txt')
            print("  .m3u не найден, используется существующий list.txt")
        if not os.path.exists(list_txt_path):
            print("ОШИБКА: list.txt не найден и .m3u отсутствует.")
            print(f"  MP3 в папке: {len(mp3_in_folder)}")
            print("  Создайте .m3u или list.txt с порядком треков.")
            return
        if not full_paths:
            full_paths = parse_list_txt(list_txt_path, folder)
        total = len(full_paths)
        if total == 0:
            print("ОШИБКА: список треков пуст.")
            return
    else:
        # For --build-album or --gen-cue, we need list.txt or .m3u
        list_txt_path = os.path.join(folder, 'list.txt')
        if os.path.exists(list_txt_path):
            full_paths = parse_list_txt(list_txt_path, folder)
        else:
            # Try .m3u -> list.txt
            _, full_paths = m3u_to_list_txt(folder)
            if not full_paths:
                print("ОШИБКА: list.txt не найден и .m3u отсутствует.")
                return
        total = len(full_paths)

    # ── Tagging step (skip if --build-album or --gen-cue) ───
    errors = 0
    if not skip_tagging:
        print(f"\n[2/3] Тегирование треков ...")
        show_total = cfg.get('show_total_tracks', 'yes').lower() in ('yes', '1', 'true')
        for i, fp in enumerate(full_paths, start=1):
            if not os.path.exists(fp):
                print(f"  [{i:>3}/{total}] НЕ НАЙДЕН: {fp}")
                errors += 1
                continue
            title     = os.path.splitext(os.path.basename(fp))[0]
            track_str = f"{i}/{total}" if show_total else str(i)
            try:
                tag_file(fp, title, track_str, cfg, cover_data, cover_mime)
                print(f"  [{i:>3}/{total}] OK  {title}")
            except Exception:
                print(f"  [{i:>3}/{total}] ОШИБКА: {fp}")
                traceback.print_exc()
                errors += 1
    else:
        print(f"\n[2/3] Тегирование пропущено (режим --build-album или --gen-cue)")

    # ── Album MP3 + CUE (skip for --tag-only) ──────────────
    if not tag_only:
        album_name     = cfg.get('album', '').strip() or os.path.basename(folder)
        safe_album     = re.sub(r'[\\/*?:"<>|]', '_', album_name) or 'album'
        album_mp3_name = f"{safe_album}.mp3"
        cue_path       = os.path.join(folder, f"{safe_album}.cue")

        print(f"\n[3/3] CUE и альбомный MP3 ...")
        existing   = [p for p in full_paths if os.path.exists(p)]
        track_data = collect_track_data(existing)

        cue_exists = os.path.exists(cue_path)
        album_exists = os.path.exists(os.path.join(folder, album_mp3_name))

        if build_album or (not cue_exists and not gen_cue):
            build_album_mp3(track_data, folder, album_name, cover_data, cover_mime)
        else:
            print(f"  Альбомный MP3 уже существует: {album_mp3_name}")

        if gen_cue or not cue_exists:
            if not cue_exists:
                cue_text = build_cue(track_data, cfg, album_mp3_name)
                with open(cue_path, 'w', encoding='utf-8-sig') as f:
                    f.write(cue_text)
                print(f"  CUE создан: {os.path.basename(cue_path)}")
            else:
                print(f"  CUE найден, обновление метаданных ...")
                update_cue_metadata(cue_path, cfg)
                print(f"  CUE обновлён: {os.path.basename(cue_path)}")
    else:
        album_mp3_name = '--tag-only, пропущено'
        cue_path = ''
        total_dur = 0

    # ── Summary ─────────────────────────────────────────────
    if not tag_only:
        total_dur = sum(t['duration'] for t in track_data)
        m, s = divmod(int(total_dur), 60)
        h, m2 = divmod(m, 60)
        dur_str = f"{h}:{m2:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    else:
        dur_str = '—'

    cover_status = 'с обложкой' if cover_data else '(без обложки)'

    print(f"\n{'='*50}")
    print(f"ПАПКА:        {folder}")
    print(f"Треки:        {total - errors}/{total}  (ошибок: {errors})")
    print(f"Обложка:      {cover_status}")
    print(f"Альбомный MP3: {album_mp3_name}")
    print(f"CUE sheet:    {cue_path if cue_path else '—'}")
    print(f"Длительность: {dur_str}")
    if errors:
        print(f"\n⚠  {errors} ошибок — проверьте выше.")
    if tag_only:
        print("  (режим --tag-only: альбом и CUE не создавались)")


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(f"\n{'='*50}")
        print("КРИТИЧЕСКАЯ ОШИБКА: Непредвиденная ошибка при выполнении.")
        print()
        traceback.print_exc()
        print(f"\n{'='*50}")
        sys.exit(1)