import os
import stat
import hashlib
from collections import defaultdict
import sys
import tkinter as tk
from tkinter import messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shell_arg import shell_target
from verified_file import delete_verified_duplicate

def is_reparse(path):
    try:
        info = os.lstat(path)
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
    except OSError:
        return True

def hash_file_partial(path, chunk_size=65536):
    """   64  ( )"""
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        chunk = f.read(chunk_size)
        if chunk:
            hasher.update(chunk)
    return hasher.hexdigest()

def hash_file(path, block_size=65536):
    """   """
    hasher = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(block_size):
            hasher.update(chunk)
    return hasher.hexdigest()

def main():
    if len(sys.argv) < 2:
        return

    root_dir = os.path.abspath(shell_target(sys.argv[1]))
    if not os.path.isdir(root_dir) or is_reparse(root_dir):
        return

    size_dict = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root_dir, topdown=True):
        dirnames[:] = [d for d in dirnames if not is_reparse(os.path.join(dirpath, d))]
        for name in filenames:
            filepath = os.path.join(dirpath, name)
            if is_reparse(filepath):
                continue
            try:
                if os.path.commonpath([root_dir, os.path.abspath(filepath)]) != root_dir:
                    continue
                size = os.path.getsize(filepath)
                size_dict[size].append(filepath)
            except OSError:
                continue

    duplicates = []

    for files in size_dict.values():
        if len(files) < 2:
            continue
            
        #  1:   ( 64)
        partial_hashes = defaultdict(list)
        for f in files:
            try:
                ph = hash_file_partial(f)
                partial_hashes[ph].append(f)
            except OSError:
                continue
                
        #  2:   (   )
        for ph_files in partial_hashes.values():
            if len(ph_files) < 2:
                continue
                
            full_hashes = {}
            for f in ph_files:
                try:
                    h = hash_file(f)
                    if h in full_hashes:
                        # Carry the digest that qualified this path, and the
                        # copy being kept, into the deletion phase. Without them
                        # the delete step has only a path string and cannot tell
                        # whether the bytes it is destroying are still the ones
                        # that were proven redundant.
                        duplicates.append((f, h, full_hashes[h]))
                    else:
                        full_hashes[h] = f
                except OSError:
                    continue

    root = tk.Tk()
    root.withdraw()
    if duplicates:
        paths = [path for path, _, _ in duplicates]
        total_bytes = sum(os.path.getsize(f) for f in paths if os.path.exists(f))
        preview = "\n".join(paths[:10])
        if len(paths) > 10:
            preview += f"\n... и ещё {len(paths) - 10}"
        confirmed = messagebox.askyesno(
            "Удалить дубликаты?",
            f"Найдено лишних копий: {len(paths)}\n"
            f"Можно освободить: {round(total_bytes / 1024 / 1024, 2)} МБ\n\n"
            "Первая копия каждого файла останется. Корзина не используется.\n\n"
            f"Будут удалены:\n{preview}\n\nПродолжить?",
        )
        if not confirmed:
            return
        deleted_count = 0
        saved_bytes = 0
        skipped = []
        # The confirmation dialog is an unbounded pause: a candidate can be
        # edited, replaced or swapped for something else entirely while it sits
        # open. This worker bypasses the Recycle Bin, so deleting on the strength
        # of a stale hash is unrecoverable data loss. Every candidate is proven
        # again, immediately before it is destroyed.
        for path, digest, kept in duplicates:
            try:
                if not os.path.exists(path):
                    skipped.append(path)
                    continue
                size = delete_verified_duplicate(path, kept, digest, scope=root_dir)
                deleted_count += 1
                saved_bytes += size
            except OSError:
                skipped.append(path)
                continue
        mb = round(saved_bytes / 1024 / 1024, 2)
        msg = f"Удалено дубликатов: {deleted_count}\nОсвобождено: {mb} МБ"
        if skipped:
            preview_skipped = "\n".join(skipped[:5])
            if len(skipped) > 5:
                preview_skipped += f"\n... и ещё {len(skipped) - 5}"
            msg += (f"\n\nПропущено (изменились или недоступны): {len(skipped)}\n"
                    f"{preview_skipped}")
        messagebox.showinfo("Готово", msg)
    else:
        messagebox.showinfo("Результат", "Дубликаты не найдены.")

if __name__ == "__main__":
    main()

