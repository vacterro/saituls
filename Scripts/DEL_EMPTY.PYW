import os
import sys
import tkinter as tk
from tkinter import messagebox


def remove_empty_dirs(path):
    removed = 0
    removed_dirs = []
    failed = []

    for root, dirs, files in os.walk(path, topdown=False):
        for dir in dirs:
            full_path = os.path.join(root, dir)
            if not os.listdir(full_path):
                try:
                    os.rmdir(full_path)
                    removed_dirs.append(full_path)
                    removed += 1
                except Exception as e:
                    failed.append((full_path, str(e)))

    # Check the root path itself
    if os.path.isdir(path) and not os.listdir(path):
        try:
            os.rmdir(path)
            removed_dirs.append(path)
            removed += 1
        except Exception as e:
            failed.append((path, str(e)))

    return removed, removed_dirs, failed


if __name__ == '__main__':
    if len(sys.argv) > 1:
        current_dir = sys.argv[1].strip('"')
    else:
        current_dir = os.getcwd()

    root = tk.Tk()
    root.withdraw()
    if not messagebox.askyesno(
        "Удалить пустые папки?",
        f"Все пустые папки внутри этого пути будут удалены без корзины:\n\n{current_dir}\n\nПродолжить?",
    ):
        raise SystemExit(0)

    total, deleted_paths, failed = remove_empty_dirs(current_dir)

    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, "_.txt")

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"Удалено пустых папок: {total}\n\n")
            for path in deleted_paths:
                f.write(path + "\n")
            for path, err in failed:
                f.write(f"ОШИБКА: {path} -- {err}\n")
    except Exception as e:
        messagebox.showerror("Ошибка", f"Не удалось записать лог:\n{e}")

    if total > 0:
        msg = f"Удалено папок: {total}\nЛог: {log_path}"
        if failed:
            msg += f"\nНе удалось удалить: {len(failed)}"
        messagebox.showinfo("Готово", msg)
    else:
        msg = "Пустые папки не найдены."
        if failed:
            msg += f"\nНе удалось удалить: {len(failed)}"
        messagebox.showinfo("Результат", msg)
