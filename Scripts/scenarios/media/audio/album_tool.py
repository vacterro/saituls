# ALBUM_TOOL.py  v1
# Dark Golden Win95 vintage GUI for MP3 album playlist management
# Deps: pip install mutagen  (tkinter is built-in)
#
# Usage:  python ALBUM_TOOL.py [folder]

import sys, os, shutil, subprocess, threading, queue
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SCRIPT_DIR = Path(__file__).parent.resolve()
TAG_MP3_PATH = SCRIPT_DIR / 'TAG_MP3.py'
MP3_WIN_PATH = SCRIPT_DIR / 'MP3_WIN.py'
INI_PATH     = SCRIPT_DIR / 'album_tags.ini'

# Check mutagen at startup so the user sees a clear error
_MUTAGEN_OK = True
try:
    import mutagen
except ImportError:
    _MUTAGEN_OK = False

if not _MUTAGEN_OK:
    print("WARNING: mutagen not installed. Install: pip install mutagen")

# Dark Golden Win95 color tokens
C = {
    'bg':'#1A0F05','bgSoft':'#1E1408','surface':'#2A1C0A',
    'surfaceRaised':'#362812','surfaceAlt':'#3A2A15',
    'borderDark':'#0E0803','borderHi':'#C0A060','borderMuted':'#4A3820',
    'text':'#D4B87A','textSec':'#B09558','textMuted':'#7A6838',
    'sel':'#362812','btnFace':'#3A2A15','btnText':'#D4B87A',
    'entryBg':'#1E1408','entryFg':'#D4B87A',
    'logBg':'#0F0A04','logFg':'#C0A060',
    'success':'#4A7A20','warning':'#7A7A20','danger':'#7A2020',
}


class Worker(threading.Thread):
    def __init__(self, cmd, cwd, log_queue):
        super().__init__(daemon=True)
        self.cmd = cmd
        self.cwd = cwd
        self.log_queue = log_queue
        self._stop = threading.Event()
    def stop(self):
        self._stop.set()
    def run(self):
        try:
            proc = subprocess.Popen(self.cmd, cwd=self.cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            for line in proc.stdout:
                if self._stop.is_set():
                    proc.terminate()
                    break
                self.log_queue.put(('out', line.rstrip()))
            proc.wait()
            self.log_queue.put(('done', proc.returncode))
        except Exception as e:
            self.log_queue.put(('err', str(e)))
            self.log_queue.put(('done', -1))


class AlbumTool:
    def __init__(self, root):
        self.root = root
        self.root.title('Album Playlist Tool')
        self.root.configure(bg=C['bg'])
        self.root.resizable(True, True)
        self.root.minsize(640, 480)
        self.root.geometry('720x540+100+100')
        self.folder_path = ''
        self.track_list = []
        self.worker = None
        self.log_queue = queue.Queue()
        self.polling = False
        self._build_ui()
        self._poll_queue()
        if len(sys.argv) > 1:
            self.set_folder(sys.argv[1])

    def _build_ui(self):
        # Menu bar
        mb = tk.Frame(self.root, bg=C['bgSoft'], bd=0, height=22)
        mb.pack(fill='x')
        for label, cmd in [('File', self._menu_file), ('Tools', self._menu_tools), ('Help', self._menu_help)]:
            b = tk.Button(mb, text=label, font=('Microsoft Sans Serif',11),
                bg=C['surface'], fg=C['text'], activebackground=C['surfaceAlt'],
                activeforeground=C['text'], bd=1, padx=6, pady=0, highlightthickness=0, command=cmd)
            b.pack(side='left', padx=(1,0), pady=1)

        # Folder selection bar
        top = tk.Frame(self.root, bg=C['surface'], bd=0,
            highlightbackground=C['borderHi'], highlightcolor=C['borderDark'], highlightthickness=1)
        top.pack(fill='x', padx=4, pady=(4,2))
        tk.Label(top, text='Folder:', bg=C['surface'], fg=C['text'],
            font=('Microsoft Sans Serif',11)).pack(side='left', padx=(4,2), pady=4)

        self.folder_var = tk.StringVar()
        self.folder_entry = tk.Entry(top, textvariable=self.folder_var,
            font=('Microsoft Sans Serif',11), bg=C['entryBg'], fg=C['entryFg'],
            insertbackground=C['text'], bd=1, relief='sunken', state='readonly',
            readonlybackground=C['entryBg'])
        self.folder_entry.pack(side='left', fill='x', expand=True, padx=(0,4), pady=4)

        tk.Button(top, text='Browse...', font=('Microsoft Sans Serif',11),
            bg=C['btnFace'], fg=C['btnText'], bd=1, padx=8, pady=1,
            command=lambda: self.set_folder(filedialog.askdirectory(title='Select Album Folder'))
            ).pack(side='left', padx=(0,2), pady=4)

        # Notebook (tabs)
        style = ttk.Style()
        style.theme_use('default')
        style.configure('TNotebook', background=C['bgSoft'], borderwidth=0)
        style.configure('TNotebook.Tab', background=C['surface'], foreground=C['text'],
            padding=(8,2), font=('Microsoft Sans Serif',11))
        style.map('TNotebook.Tab',
            background=[('selected', C['surfaceAlt']), ('active', C['surfaceRaised'])],
            foreground=[('selected', C['borderHi'])])

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, padx=4, pady=2)

        # Tab 1: Workflow
        tab_work = tk.Frame(self.notebook, bg=C['surface'])
        self.notebook.add(tab_work, text='Workflow')
        self._build_workflow(tab_work)

        # Tab 2: Settings
        tab_sett = tk.Frame(self.notebook, bg=C['surface'])
        self.notebook.add(tab_sett, text='Settings')
        self._build_settings(tab_sett)

        # Tab 3: Tracks preview
        tab_pre = tk.Frame(self.notebook, bg=C['surface'])
        self.notebook.add(tab_pre, text='Tracks')
        self._build_preview(tab_pre)

        # Log output
        log_frame = tk.Frame(self.root, bg=C['borderDark'], bd=2, relief='sunken')
        log_frame.pack(fill='both', padx=4, pady=(2,4))

        hdr = tk.Frame(log_frame, bg=C['surface'])
        hdr.pack(fill='x')
        tk.Label(hdr, text='Output Log', bg=C['surface'], fg=C['textSec'],
            font=('Microsoft Sans Serif',11)).pack(side='left', padx=4, pady=1)
        tk.Button(hdr, text='Clear', font=('Microsoft Sans Serif',11),
            bg=C['btnFace'], fg=C['btnText'], bd=1, padx=4, pady=0,
            command=self._clear_log).pack(side='right', padx=4, pady=1)

        self.log_text = tk.Text(log_frame, height=8,
            bg=C['logBg'], fg=C['logFg'], insertbackground=C['text'],
            font=('Courier New',10), bd=0, highlightthickness=0, wrap='word', state='disabled')
        self.log_text.pack(fill='both', expand=True, padx=2, pady=2)

        log_scroll = tk.Scrollbar(self.log_text, command=self.log_text.yview)
        log_scroll.pack(side='right', fill='y')
        self.log_text.config(yscrollcommand=log_scroll.set)

        # Status bar
        self.status_var = tk.Label(self.root, text='Ready', anchor='w',
            bg=C['bgSoft'], fg=C['textMuted'], font=('Microsoft Sans Serif',10), padx=4, pady=1)
        self.status_var.pack(fill='x')

    def _build_workflow(self, parent):
        info = tk.Frame(parent, bg=C['entryBg'], bd=2, relief='sunken')
        info.pack(fill='x', padx=4, pady=4)
        self.info_label = tk.Label(info, text='Select a folder to begin.',
            bg=C['entryBg'], fg=C['textSec'], font=('Microsoft Sans Serif',11),
            anchor='w', justify='left', padx=4, pady=6)
        self.info_label.pack(fill='x')

        actions = tk.Frame(parent, bg=C['surface'], bd=2, relief='raised')
        actions.pack(fill='both', expand=True, padx=4, pady=4)

        btn_frame = tk.Frame(actions, bg=C['surface'])
        btn_frame.pack(expand=True, padx=8, pady=8)

        btns = [
            ('Run Full Workflow', self._run_workflow),
            ('Tag Only (ID3v2)', self._run_tag_only),
            ('Build Album MP3', self._run_build_album),
            ('Generate CUE Sheet', self._run_gen_cue),
        ]
        for i, (label, cmd) in enumerate(btns):
            b = tk.Button(btn_frame, text=label, font=('Microsoft Sans Serif',11),
                bg=C['btnFace'], fg=C['btnText'], bd=2, relief='raised',
                padx=12, pady=4, activebackground=C['surfaceAlt'],
                activeforeground=C['text'], command=cmd)
            b.grid(row=i//2, column=i%2, padx=8, pady=6, sticky='ew')
            btn_frame.columnconfigure(i%2, weight=1)

        # Progress bar area
        prog_frame = tk.Frame(parent, bg=C['surface'])
        prog_frame.pack(fill='x', padx=4, pady=(0,4))
        self.progress = ttk.Progressbar(prog_frame, mode='indeterminate', length=300)

    def _build_settings(self, parent):
        self.sett_text = tk.Text(parent,
            bg=C['entryBg'], fg=C['entryFg'], insertbackground=C['text'],
            font=('Courier New',11), bd=2, relief='sunken', highlightthickness=0, wrap='none')
        self.sett_text.pack(side='left', fill='both', expand=True, padx=4, pady=4)
        scroll_y = tk.Scrollbar(self.sett_text, command=self.sett_text.yview)
        scroll_y.pack(side='right', fill='y')
        self.sett_text.config(yscrollcommand=scroll_y.set)
        scroll_x = tk.Scrollbar(orient='horizontal', command=self.sett_text.xview)
        scroll_x.pack(side='bottom', fill='x')
        self.sett_text.config(xscrollcommand=scroll_x.set)

        btn_row = tk.Frame(parent, bg=C['surface'])
        btn_row.pack(fill='x', padx=4, pady=(0,4))
        tk.Button(btn_row, text='Load from disk', font=('Microsoft Sans Serif',11),
            bg=C['btnFace'], fg=C['btnText'], bd=1, padx=6, pady=1,
            command=self._load_ini).pack(side='left', padx=(0,4))
        tk.Button(btn_row, text='Save to disk', font=('Microsoft Sans Serif',11),
            bg=C['btnFace'], fg=C['btnText'], bd=1, padx=6, pady=1,
            command=self._save_ini).pack(side='left')
        tk.Button(btn_row, text='Edit in Notepad', font=('Microsoft Sans Serif',11),
            bg=C['btnFace'], fg=C['btnText'], bd=1, padx=6, pady=1,
            command=self._edit_ini_ext).pack(side='left', padx=(4,0))

    def _build_preview(self, parent):
        self.preview_list = tk.Listbox(parent,
            bg=C['entryBg'], fg=C['entryFg'], font=('Courier New',10),
            bd=2, relief='sunken', highlightthickness=0,
            selectbackground=C['sel'], selectforeground=C['text'])
        self.preview_list.pack(side='left', fill='both', expand=True, padx=4, pady=4)
        scroll = tk.Scrollbar(self.preview_list, command=self.preview_list.yview)
        scroll.pack(side='right', fill='y')
        self.preview_list.config(yscrollcommand=scroll.set)

    # ── Menu actions ────────────────────────────────────────
    def _menu_file(self):
        self.set_folder(filedialog.askdirectory(title='Select Album Folder'))

    def _menu_tools(self):
        win = tk.Toplevel(self.root)
        win.title('Tools')
        win.configure(bg=C['surface'])
        ox = self.root.winfo_x() + 80
        oy = self.root.winfo_y() + 80
        win.geometry('320x140+{}+{}'.format(ox, oy))
        win.transient(self.root)
        win.grab_set()
        tk.Label(win, text='Quick actions:', bg=C['surface'], fg=C['text'],
            font=('Microsoft Sans Serif',11)).pack(padx=8, pady=6, anchor='w')
        tk.Button(win, text='Open folder in Explorer', font=('Microsoft Sans Serif',11),
            bg=C['btnFace'], fg=C['btnText'], bd=1, padx=4, pady=1,
            command=lambda: [win.destroy(), self._open_explorer()]
            ).pack(padx=8, pady=2, fill='x')
        tk.Button(win, text='Edit album_tags.ini externally', font=('Microsoft Sans Serif',11),
            bg=C['btnFace'], fg=C['btnText'], bd=1, padx=4, pady=1,
            command=lambda: [win.destroy(), self._edit_ini_ext()]
            ).pack(padx=8, pady=2, fill='x')
        tk.Button(win, text='Rename MP3 by Title tag', font=('Microsoft Sans Serif',11),
            bg=C['btnFace'], fg=C['btnText'], bd=1, padx=4, pady=1,
            command=lambda: [win.destroy(), self._run_rename()]
            ).pack(padx=8, pady=2, fill='x')

    def _menu_help(self):
        mut = 'Installed' if _MUTAGEN_OK else 'MISSING!'
        ff = 'Found' if shutil.which('ffmpeg') else 'NOT IN PATH'
        messagebox.showinfo('About',
            'Album Playlist Tool v1\n'
            'Dark Golden Windows 95 vintage GUI\n\n'
            'Workflow:\n'
            '1. Select folder with MP3 files\n'
            '2. Ensure list.txt or .m3u exists\n'
            '3. Configure album_tags.ini\n'
            '4. Click Run Full Workflow\n'
            '5. Tags, cover, album MP3, CUE created\n\n'
            'Dependencies:\n'
            '  mutagen: ' + mut + ' (ID3v2 tagging)\n'
            '  ffmpeg:  ' + ff + ' (album MP3 assembly)',
            parent=self.root)

    # ── Folder setter ───────────────────────────────────────
    def set_folder(self, path):
        if not path:
            return
        self.folder_path = os.path.normpath(path)
        self.folder_var.set(self.folder_path)
        self._refresh_preview()
        self._load_ini()
        self._update_info()
        self.log('Folder set: ' + self.folder_path)

    def _update_info(self):
        if not self.folder_path:
            self.info_label.config(text='Select a folder to begin.')
            return
        folder = Path(self.folder_path)
        mp3s = list(folder.glob('*.mp3'))
        m3us = list(folder.glob('*.m3u'))
        has_list = (folder / 'list.txt').is_file()
        has_cover = any((folder / n).is_file() for n in ['cover.png','cover.jpg','folder.jpg'])
        parts = []
        parts.append('Folder: ' + folder.name)
        parts.append('MP3: ' + str(len(mp3s)))
        parts.append('list.txt: ' + ('YES' if has_list else 'NO'))
        parts.append('.m3u: ' + str(len(m3us)))
        parts.append('Cover: ' + ('YES' if has_cover else 'NO'))
        if not has_list and not m3us:
            parts.append('Need list.txt or .m3u')
        self.info_label.config(text='  |  '.join(parts))

    # ── Preview ─────────────────────────────────────────────
    def _refresh_preview(self):
        self.preview_list.delete(0, 'end')
        self.track_list = []
        if not self.folder_path:
            return
        try:
            folder = Path(self.folder_path)
            list_path = folder / 'list.txt'
            if list_path.is_file():
                self._load_tracks_from_list(str(list_path))
            else:
                m3us = sorted(folder.glob('*.m3u'))
                if m3us:
                    self._load_tracks_from_m3u(str(m3us[0]))
                else:
                    for f in sorted(folder.glob('*.mp3')):
                        self.preview_list.insert('end', '  ' + f.name)
                        self.track_list.append(str(f))
        except Exception as e:
            self.log('Error refreshing preview: ' + str(e))

    def _load_tracks_from_list(self, list_path):
        try:
            import TAG_MP3 as tm
        except ImportError:
            self.log('ERROR: Could not load TAG_MP3 module. Check dependencies.')
            return
        paths = tm.parse_list_txt(list_path, self.folder_path)
        for i, p in enumerate(paths, 1):
            if os.path.exists(p):
                name = os.path.basename(p)
            else:
                name = '[MISSING] ' + os.path.basename(p)
            self.preview_list.insert('end', '{:3d}. {}'.format(i, name))
            self.track_list.append(p)

    def _load_tracks_from_m3u(self, m3u_path):
        m3u = Path(m3u_path)
        try:
            lines = m3u.read_text(encoding='utf-8-sig', errors='replace').splitlines()
        except (OSError, PermissionError) as e:
            self.log('Error reading .m3u: ' + str(e))
            return
        i = 1
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            p = Path(line)
            if not p.is_absolute():
                p = Path(self.folder_path) / line
            if p.exists():
                name = p.name
            else:
                name = '[MISSING] ' + p.name
            self.preview_list.insert('end', '{:3d}. {}'.format(i, name))
            self.track_list.append(str(p))
            i += 1

    # ── INI handling ────────────────────────────────────────
    def _load_ini(self):
        path = INI_PATH
        if not path.is_file():
            self.sett_text.delete('1.0', 'end')
            self.sett_text.insert('1.0', '; album_tags.ini not found')
            return
        try:
            txt = path.read_text(encoding='utf-8')
            self.sett_text.delete('1.0', 'end')
            self.sett_text.insert('1.0', txt)
        except Exception as e:
            self.log('Error loading INI: ' + str(e))

    def _save_ini(self):
        try:
            txt = self.sett_text.get('1.0', 'end-1c')
            INI_PATH.write_text(txt, encoding='utf-8')
            self.log('album_tags.ini saved.')
            messagebox.showinfo('Saved', 'album_tags.ini saved.', parent=self.root)
        except Exception as e:
            self.log('Error saving INI: ' + str(e))
            messagebox.showerror('Error', 'Could not save: ' + str(e), parent=self.root)

    def _edit_ini_ext(self):
        if INI_PATH.is_file():
            os.startfile(str(INI_PATH))
        else:
            messagebox.showinfo('Not found', 'album_tags.ini not found.', parent=self.root)

    # ── Logging ──────────────────────────────────────────────
    def log(self, msg):
        self.log_text.config(state='normal')
        self.log_text.insert('end', msg + chr(10))
        self.log_text.see('end')
        self.log_text.config(state='disabled')

    def _clear_log(self):
        self.log_text.config(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.config(state='disabled')

    def _set_status(self, msg):
        self.status_var.config(text=msg)
        self.root.update_idletasks()

    # ── Queue polling ───────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                typ, data = self.log_queue.get_nowait()
                if typ == 'out':
                    self.log(data)
                elif typ == 'done':
                    code = data
                    if code == 0:
                        self.log('-- Completed OK --')
                        self._set_status('Ready')
                    else:
                        self.log('-- Exit code ' + str(code) + ' --')
                        self._set_status('Error')
                    self.polling = False
                    self._enable_buttons(True)
                    self.progress.stop()
                elif typ == 'err':
                    self.log('ERROR: ' + str(data))
        except queue.Empty:
            pass
        if self.polling:
            self.root.after(100, self._poll_queue)

    def _enable_buttons(self, enabled):
        state = 'normal' if enabled else 'disabled'
        for child in self.root.winfo_children():
            self._set_state(child, state)

    def _set_state(self, widget, state):
        try:
            if isinstance(widget, (tk.Button, ttk.Button)):
                widget.config(state=state)
        except Exception:
            pass
        try:
            for child in widget.winfo_children():
                self._set_state(child, state)
        except Exception:
            pass

    # ── Run commands ────────────────────────────────────────
    def _run_python(self, script, args=None, label=''):
        if not self.folder_path:
            messagebox.showwarning('No Folder', 'Select a folder first.', parent=self.root)
            return
        cmd = [sys.executable, str(script), self.folder_path]
        if args:
            cmd.extend(args)
        self.log('> Running: ' + ' '.join(cmd[-3:]))
        self._set_status('Running ' + label + '...')
        self.polling = True
        self._enable_buttons(False)
        self.progress.pack(pady=4)
        self.progress.start()
        self.worker = Worker(cmd, self.folder_path, self.log_queue)
        self.worker.start()

    def _run_workflow(self):
        self._run_python(TAG_MP3_PATH, label='Full Workflow')

    def _run_tag_only(self):
        self._run_python(TAG_MP3_PATH, ['--tag-only'], label='Tag Only')

    def _run_build_album(self):
        self._run_python(TAG_MP3_PATH, ['--build-album'], label='Build Album')

    def _run_gen_cue(self):
        self._run_python(TAG_MP3_PATH, ['--gen-cue'], label='Gen CUE')

    def _run_rename(self):
        if not self.folder_path:
            messagebox.showwarning('No Folder', 'Select a folder first.', parent=self.root)
            return
        self.log('> Renaming MP3 by Title tag...')
        self._set_status('Renaming...')
        self.polling = True
        self._enable_buttons(False)
        self.progress.pack(pady=4)
        self.progress.start()
        cmd = [sys.executable, str(MP3_WIN_PATH), self.folder_path]
        self.worker = Worker(cmd, self.folder_path, self.log_queue)
        self.worker.start()

    def _open_explorer(self):
        if self.folder_path and os.path.isdir(self.folder_path):
            os.startfile(self.folder_path)


def main():
    root = tk.Tk()
    app = AlbumTool(root)
    root.mainloop()

if __name__ == '__main__':
    main()
