import os
import sys
import shutil
import tkinter as tk
from tkinter import messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shell_arg import shell_target


def pack_into_folder(target_path):
    if not os.path.exists(target_path):
        return
        
    parent_dir = os.path.dirname(target_path)
    base_name = os.path.basename(target_path)
    
    new_folder_name = base_name + "_Packed"
    new_folder_path = os.path.join(parent_dir, new_folder_name)
    
    counter = 1
    while os.path.exists(new_folder_path):
        new_folder_name = f"{base_name}_Packed_{counter}"
        new_folder_path = os.path.join(parent_dir, new_folder_name)
        counter += 1
        
    try:
        os.makedirs(new_folder_path)
        shutil.move(target_path, os.path.join(new_folder_path, base_name))
        
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("Готово", f"Успешно упаковано в:\n{new_folder_name}")
    except Exception as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Ошибка", f"Произошла ошибка:\n{e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = shell_target(sys.argv[1])
        # Packing a drive root would mean moving the whole volume into a
        # subfolder of itself. The menu is registered on files and folders only,
        # so this is a guard, not a reachable path -- but this worker moves data.
        if os.path.splitdrive(target)[1] in ("", os.sep):
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Ошибка", "Нельзя упаковать корень диска.")
        else:
            pack_into_folder(target)

