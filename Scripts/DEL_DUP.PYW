import os
import hashlib
from collections import defaultdict
import sys
import tkinter as tk
from tkinter import messagebox

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

    root_dir = sys.argv[1].strip('"')
    if not os.path.isdir(root_dir):
        return

    size_dict = defaultdict(list)
    for dirpath, _, filenames in os.walk(root_dir):
        for name in filenames:
            filepath = os.path.join(dirpath, name)
            try:
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
                        duplicates.append(f)
                    else:
                        full_hashes[h] = f
                except OSError:
                    continue

    root = tk.Tk()
    root.withdraw()
    if duplicates:
        total_bytes = sum(os.path.getsize(f) for f in duplicates if os.path.exists(f))
        preview = "\n".join(duplicates[:10])
        if len(duplicates) > 10:
            preview += f"\n... и ещё {len(duplicates) - 10}"
        confirmed = messagebox.askyesno(
            "Удалить дубликаты?",
            f"Найдено лишних копий: {len(duplicates)}\n"
            f"Можно освободить: {round(total_bytes / 1024 / 1024, 2)} МБ\n\n"
            "Первая копия каждого файла останется. Корзина не используется.\n\n"
            f"Будут удалены:\n{preview}\n\nПродолжить?",
        )
        if not confirmed:
            return
        deleted_count = 0
        saved_bytes = 0
        for path in duplicates:
            try:
                size = os.path.getsize(path)
                os.remove(path)
                deleted_count += 1
                saved_bytes += size
            except OSError:
                continue
        mb = round(saved_bytes / 1024 / 1024, 2)
        messagebox.showinfo("Готово", f"Удалено дубликатов: {deleted_count}\nОсвобождено: {mb} МБ")
    else:
        messagebox.showinfo("Результат", "Дубликаты не найдены.")

if __name__ == "__main__":
    main()

