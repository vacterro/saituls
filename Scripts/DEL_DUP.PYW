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

    deleted_count = 0
    saved_bytes = 0
    
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
                        size = os.path.getsize(f)
                        os.remove(f)
                        deleted_count += 1
                        saved_bytes += size
                    else:
                        full_hashes[h] = f
                except OSError:
                    continue

    root = tk.Tk()
    root.withdraw()
    if deleted_count > 0:
        mb = round(saved_bytes / 1024 / 1024, 2)
        messagebox.showinfo("Готово", f"Удалено дубликатов: {deleted_count}\nОсвобождено: {mb} МБ")
    else:
        messagebox.showinfo("Результат", "Дубликаты не найдены.")

if __name__ == "__main__":
    main()

