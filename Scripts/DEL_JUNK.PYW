"""DEL JUNK - рекурсивный снос расширений и паттернов откровенного мусора.

Списки JUNK_EXTENSIONS / JUNK_DIR_NAMES / *_PATTERNS собраны из
_SMART_VAC_CLEANER.py (папки cache/webcache/crashpad/gpucache/__pycache__/
.pytest_cache/.ruff_cache, профиль-мусор LOCK/LOG/LOG.old, .tmp/.exe.tmp из
%LOCALAPPDATA% и т.п.) + общеизвестные junk-расширения разработки/IDE/
браузеров/загрузок. Запускается через pythonw.exe без консоли.
"""

import os
import shutil
import sys
import tkinter as tk
from tkinter import messagebox

JUNK_EXTENSIONS = {
    # temp / incomplete transfers — always junk
    ".tmp", ".temp", ".part", ".crdownload", ".download", ".opdownload",
    # regenerable build / bytecode artifacts
    ".pyc", ".pyo", ".o", ".obj", ".ilk", ".idb", ".pch", ".sdf",
    ".bsc", ".ncb", ".suo",
    # crash / trace dumps — regenerable, no data value
    ".dmp", ".mdmp", ".minidump", ".crash", ".etl", ".wpr", ".wpa",
    ".blg", ".evtx",
    # editor litter
    ".swp", ".swo", ".swn",
    # OS / explorer litter
    ".ds_store", ".thumbs.db",
    # app cache payload files
    ".cache",
    # logs
    ".log",
}

# NOTE: explicitly NOT junk on purpose: .git/.svn/.hg (repo history),
# .bak/.old/.backup*/_old/-old (rollback backups, T-099), .lnk/.url
# (user shortcuts), .jar/.class/.pyd (real binaries), .pdb (debug
# symbols), .torrent/.rnd/.desktop.ini (user data / system metadata).

JUNK_DIR_NAMES = {
    # python / tooling caches — regenerable
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".hypothesis", ".cache",
    # generic cache / temp / log dirs
    "cache", "caches", "temp", "tmp", "logs", "log",
    "crashpad", "crashes", "dumps", "minidumps",
    "updater",
    # browser / app caches (exact compound names only, no substring)
    "gpucache", "shadercache", "grshadercache", "dawncache",
    "graphitedawncache", "thumbnailcache", "webcache", "cache2",
    "startupcache", "code cache", "browsermetrics", "local traces",
    "component_crx_cache", "extensions_crx_cache",
    # node / frontend dependency dirs — regenerable via package manager
    "node_modules", "bower_components",
}

# build output dirs — regenerable (Maven/Rust/etc). Exact name only.
JUNK_BUILD_OUTPUT_DIRS = {
    "target", "build", "dist",
}

JUNK_FILE_PATTERNS = [
    "*.log", "*.tmp", "*.temp", "*.swp", "*.pyc",
    "*.cache", "*.dmp", "*.crash", "*.etl",
    "thumbs.db", ".ds_store",
    "*.exe.tmp", "*.part", "*.crdownload",
    "run-log-*.zip",
]

JUNK_DIR_PATTERNS = [
    "__pycache__", ".pytest_cache", "ruff_cache",
    ".mypy_cache", "node_modules",
]


def should_delete_dir(name):
    name_lower = name.lower()
    if name_lower in JUNK_DIR_NAMES or name_lower in JUNK_BUILD_OUTPUT_DIRS:
        return True
    for pat in JUNK_DIR_PATTERNS:
        if pat.startswith("*") and pat.endswith("*"):
            if pat[1:-1] in name_lower:
                return True
        elif pat.startswith("*"):
            if name_lower.endswith(pat[1:]):
                return True
        elif pat.endswith("*"):
            if name_lower.startswith(pat[:-1]):
                return True
        else:
            if name_lower == pat:
                return True
    return False


def should_delete_file(name):
    name_lower = name.lower()
    ext = os.path.splitext(name_lower)[1]
    if ext in JUNK_EXTENSIONS:
        return True
    base = os.path.splitext(name_lower)[0]
    if base in JUNK_EXTENSIONS:
        return True
    if name_lower in {".ds_store", "thumbs.db"}:
        return True
    return False


def scan_junk(root_path):
    junk_files = []
    junk_dirs = []
    for root, dirs, files in os.walk(root_path, topdown=False):
        for d in dirs:
            full = os.path.join(root, d)
            if should_delete_dir(d):
                junk_dirs.append(full)
        for f in files:
            full = os.path.join(root, f)
            if should_delete_file(f):
                junk_files.append(full)
    return junk_files, junk_dirs


def delete_junk(junk_files, junk_dirs):
    deleted_files = []
    deleted_dirs = []
    errors = []
    for f in junk_files:
        try:
            os.remove(f)
            deleted_files.append(f)
        except Exception as e:
            errors.append((f, str(e)))
    for d in sorted(junk_dirs, key=lambda p: p.count(os.sep), reverse=True):
        if not os.path.isdir(d):
            continue
        try:
            shutil.rmtree(d, ignore_errors=False)
            deleted_dirs.append(d)
        except Exception as e:
            errors.append((d, str(e)))
    return deleted_files, deleted_dirs, errors


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_dir = sys.argv[1].strip('"')
    else:
        target_dir = os.getcwd()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, "_.txt")

    root = tk.Tk()
    root.withdraw()

    junk_files, junk_dirs = scan_junk(target_dir)

    if not junk_files and not junk_dirs:
        messagebox.showinfo("Результат", "Мусор не найден.")
        raise SystemExit(0)

    preview = (junk_files + junk_dirs)[:10]
    preview_text = "\n".join(preview)
    if len(junk_files) + len(junk_dirs) > len(preview):
        preview_text += f"\n... и ещё {len(junk_files) + len(junk_dirs) - len(preview)}"
    confirmed = messagebox.askyesno(
        "Удалить найденный мусор?",
        f"Папка: {target_dir}\n\n"
        f"Файлов: {len(junk_files)}\nПапок: {len(junk_dirs)}\n\n"
        "Будут удалены кэши, логи, временные файлы, а также папки "
        "build/dist/node_modules. Корзина не используется.\n\n"
        f"Первые найденные пути:\n{preview_text}\n\nПродолжить?",
    )
    if not confirmed:
        raise SystemExit(0)

    deleted_files, deleted_dirs, errors = delete_junk(junk_files, junk_dirs)

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"DEL JUNK — {target_dir}\n")
            f.write(f"Найдено файлов: {len(junk_files)}, папок: {len(junk_dirs)}\n")
            f.write(f"Удалено файлов: {len(deleted_files)}, папок: {len(deleted_dirs)}\n\n")
            if deleted_files:
                f.write("--- Файлы ---\n")
                for p in deleted_files:
                    f.write(p + "\n")
            if deleted_dirs:
                f.write("--- Папки ---\n")
                for p in deleted_dirs:
                    f.write(p + "\n")
            if errors:
                f.write("--- Ошибки ---\n")
                for p, err in errors:
                    f.write(f"ОШИБКА: {p} — {err}\n")
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось записать лог:\n{e}")

    result = f"Удалено файлов: {len(deleted_files)}\nУдалено папок: {len(deleted_dirs)}\nЛог: {log_path}"
    if errors:
        result += f"\nНе удалось удалить: {len(errors)}"
    messagebox.showinfo("Готово", result)
