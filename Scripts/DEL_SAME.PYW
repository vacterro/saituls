import os
import stat
import sys
import tkinter as tk
from tkinter import messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shell_arg import shell_target
from verified_file import confined_directories


def normalize(name):
    """     :  , / = """
    return name.lower().replace(" ", "_").replace("-", "_")

def is_duplicate_name(parent_name, child_name):
    """ True       """
    if parent_name == child_name:
        return True
    return normalize(parent_name) == normalize(child_name)

def allocate_destination(parent_path, item):
    """A destination in parent_path that does not exist, without ever
    overwriting anything: the natural name, then name_copy, name_copy_2, ...

    The old code fell back exactly once to `<name>_copy` and moved anyway, so a
    user's pre-existing `a_copy.txt` was silently replaced by the nested file.
    """
    dst = os.path.join(parent_path, item)
    if not os.path.exists(dst):
        return dst
    name, ext = os.path.splitext(item)
    dst = os.path.join(parent_path, f"{name}_copy{ext}")
    n = 2
    while os.path.exists(dst):
        dst = os.path.join(parent_path, f"{name}_copy_{n}{ext}")
        n += 1
    return dst


def is_reparse(path):
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def move_contents_up(inner_path, parent_path, label, scope=None):
    """   inner_path  parent_path   inner_path"""
    scope = scope or parent_path
    with confined_directories(scope, inner_path):
        items = os.listdir(inner_path)
        for item in items:
            if is_reparse(os.path.join(inner_path, item)):
                raise OSError(f"skipped reparse location: {os.path.join(inner_path, item)}")
        for item in items:
            src = os.path.join(inner_path, item)
            dst = allocate_destination(parent_path, item)
            if os.path.lexists(dst):
                raise OSError(f"destination appeared during flatten: {dst}")
            # Same-volume rename only. Never copy/traverse a directory as a
            # fallback. Windows rename refuses an occupied destination.
            os.rename(src, dst)
    with confined_directories(scope, parent_path):
        if is_reparse(inner_path):
            raise OSError(f"skipped reparse location: {inner_path}")
        # Remove only an empty shell. Never recursively delete late arrivals.
        os.rmdir(inner_path)
    return True

def flatten_duplicate_folders(path, skipped=None):
    path = os.path.abspath(path)
    skipped = skipped if skipped is not None else []

    def report(location, error):
        entry = (location, str(error))
        if entry not in skipped:
            skipped.append(entry)

    changed = True
    removed_count = 0
    while changed:
        changed = False
        pending, directories = [path], []
        while pending:
            current = pending.pop()
            try:
                with confined_directories(path, current):
                    with os.scandir(current) as entries:
                        for entry in entries:
                            if is_reparse(entry.path):
                                report(entry.path, 'skipped reparse location')
                            elif entry.is_dir(follow_symlinks=False):
                                pending.append(entry.path)
                                directories.append(entry.path)
            except OSError as error:
                report(current, error)
        for parent_path in reversed(directories):
            try:
                with confined_directories(path, parent_path):
                    d = os.path.basename(parent_path)
                    children = os.listdir(parent_path)
                    child_dirs = []
                    for child in children:
                        child_path = os.path.join(parent_path, child)
                        if is_reparse(child_path):
                            report(child_path, 'skipped reparse location')
                        elif os.path.isdir(child_path):
                            child_dirs.append(child)
                    selected = d if d in child_dirs else (child_dirs[0] if len(child_dirs) == 1 else None)
                    if selected and is_duplicate_name(d, selected):
                        if move_contents_up(os.path.join(parent_path, selected), parent_path, " ", path):
                            removed_count += 1
                            changed = True
            except OSError as error:
                report(parent_path, error)
    return removed_count

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(1)

    target_folder = shell_target(sys.argv[1])
    root = tk.Tk()
    root.withdraw()
    if not messagebox.askyesno(
        "Убрать вложенные папки с тем же именем?",
        f"Содержимое одинаково названных вложенных папок будет перенесено выше, "
        f"а пустые оболочки удалены без корзины:\n\n{target_folder}\n\nПродолжить?",
    ):
        raise SystemExit(0)
    skipped = []
    removed = flatten_duplicate_folders(target_folder, skipped)
    if skipped:
        messagebox.showwarning("Пропущено", "\n".join(f"{p}: {e}" for p, e in skipped[:20]))

    if removed > 0:
        messagebox.showinfo("Готово", f"Удалено одинаковых папок: {removed}")
    else:
        messagebox.showinfo("Результат", "Одинаковые папки не найдены.")

