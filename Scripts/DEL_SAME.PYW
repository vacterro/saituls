import os
import shutil
import stat
import sys
import tkinter as tk
from tkinter import messagebox

def remove_readonly(func, path, _):
    """  Read-only   """
    os.chmod(path, stat.S_IWRITE)
    func(path)

def normalize(name):
    """     :  , / = """
    return name.lower().replace(" ", "_").replace("-", "_")

def is_duplicate_name(parent_name, child_name):
    """ True       """
    if parent_name == child_name:
        return True
    return normalize(parent_name) == normalize(child_name)

def move_contents_up(inner_path, parent_path, label):
    """   inner_path  parent_path   inner_path"""
    for item in os.listdir(inner_path):
        src = os.path.join(inner_path, item)
        dst = os.path.join(parent_path, item)
        if os.path.exists(dst):
            name, ext = os.path.splitext(item)
            dst = os.path.join(parent_path, f"{name}_copy{ext}")
        shutil.move(src, dst)
    try:
        shutil.rmtree(inner_path, onerror=remove_readonly)
        return True
    except Exception as e:
        return False

def flatten_duplicate_folders(path):
    changed = True
    removed_count = 0
    while changed:
        changed = False
        for root, dirs, _ in os.walk(path, topdown=False):
            for d in dirs:
                parent_path = os.path.join(root, d)
                if not os.path.isdir(parent_path):
                    continue

                children = os.listdir(parent_path)
                child_dirs = [c for c in children if os.path.isdir(os.path.join(parent_path, c))]

                exact_inner = os.path.join(parent_path, d)
                if os.path.isdir(exact_inner):
                    if move_contents_up(exact_inner, parent_path, " "):
                        removed_count += 1
                        changed = True
                    continue

                if len(child_dirs) == 1:
                    child_name = child_dirs[0]
                    inner_path = os.path.join(parent_path, child_name)
                    if is_duplicate_name(d, child_name):
                        if move_contents_up(inner_path, parent_path, " "):
                            removed_count += 1
                            changed = True
    return removed_count

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)

    target_folder = sys.argv[1].strip('"')
    removed = flatten_duplicate_folders(target_folder)
    
    root = tk.Tk()
    root.withdraw()
    if removed > 0:
        messagebox.showinfo("Готово", f"Удалено одинаковых папок: {removed}")
    else:
        messagebox.showinfo("Результат", "Одинаковые папки не найдены.")

