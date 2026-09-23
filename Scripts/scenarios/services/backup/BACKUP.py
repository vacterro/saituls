import os
# [T-166 migrated from _SCENARIOS 2026-09-09] optional target folder: pass a folder as the first argument (otherwise operates next to this script, original drop-in behaviour)
import sys
import time
import shutil
import logging
import hashlib
import signal
import threading
import subprocess
import ctypes
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIGURATION - PATHS AND CORE SETTINGS
# ============================================================

SOURCE_DIR = os.environ.get('SAITULS_BACKUP_SOURCE', '')
DEST_PARENT = os.environ.get('SAITULS_BACKUP_DEST_PARENT', '')
DEST_NAME = 'Chillzone'
CHECK_INTERVAL = 5
BROWSER_DIR = os.environ.get('SAITULS_BACKUP_BROWSER', '')
MUSIC_DEST = os.environ.get('SAITULS_BACKUP_MUSIC', '')
PRIORITY_LOCK = os.environ.get('SAITULS_BACKUP_PRIORITY_LOCK', os.path.join(os.environ.get('PUBLIC', os.path.expanduser('~')), 'suno_priority.lock'))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEST_DIR = os.path.join(DEST_PARENT, DEST_NAME)
LOG_FILE = os.path.join(os.environ.get('SAITULS_SCENARIOS_STATE', os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'SAITULS', 'SCENARIOS')), 'BACKUP.log')
PID_FILE = os.path.join(os.environ.get('SAITULS_SCENARIOS_STATE', os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'SAITULS', 'SCENARIOS')), 'BACKUP.pid')

def setup_logging():
    fmt = '%(asctime)s | %(levelname)-8s | %(message)s'
    log = logging.getLogger(__name__)
    log.setLevel(logging.INFO)
    
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_FILE, 'a', 'utf-8')
    fh.setFormatter(logging.Formatter(fmt))
    log.addHandler(fh)
    
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt))
    log.addHandler(ch)
    return log

log = setup_logging()

class BackupMonitor:
    def __init__(self):
        self.stop_event = threading.Event()
        self.last_hash = None
        self.backup_counter = self._get_next_backup_id()
        
        self.process_lock = threading.Lock()
        self.active_processes = {}
        self.processing_files = set()
        
        self._elevate_process_priority()
        self._set_pid()

    def _elevate_process_priority(self):
        """
        Forces Windows to grant this Python script absolute top CPU priority (0x00000080).
        This guarantees the monitor never starves, even if SVT-AV1 is using 100% CPU.
        """
        try:
            if os.name == 'nt':
                # 0x00000080 is HIGH_PRIORITY_CLASS
                ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080)
                log.info("System Engine successfully elevated to HIGH_PRIORITY_CLASS.")
        except Exception as e:
            log.warning(f"Could not elevate process priority. Running standard: {e}")

    def _set_pid(self):
        try:
            with open(PID_FILE, 'w') as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

    def stop(self):
        log.info("Initiating graceful shutdown...")
        self.stop_event.set()
        
        with self.process_lock:
            for file_path, process in list(self.active_processes.items()):
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except Exception:
                    pass
                    
        if os.path.exists(PID_FILE):
            try: os.remove(PID_FILE)
            except Exception: pass
            
        if os.path.exists(PRIORITY_LOCK):
            try: os.remove(PRIORITY_LOCK)
            except Exception: pass
            
        log.info("Shutdown complete.")

    def _get_next_backup_id(self):
        # Find the highest existing Chillzone_N folder
        highest = 0
        if os.path.exists(DEST_PARENT):
            for item in os.listdir(DEST_PARENT):
                if item.startswith(DEST_NAME + '_'):
                    try:
                        num = int(item.split('_')[-1])
                        highest = max(highest, num)
                    except ValueError:
                        pass
        return highest + 1

    def hash_directory(self, directory: str) -> str:
        if not os.path.exists(directory):
            return ""
        
        hasher = hashlib.md5()
        for root, _, files in os.walk(directory):
            for name in sorted(files):
                filepath = os.path.join(root, name)
                try:
                    stat = os.stat(filepath)
                    hasher.update(name.encode('utf-8'))
                    hasher.update(str(stat.st_mtime).encode('utf-8'))
                    hasher.update(str(stat.st_size).encode('utf-8'))
                except (PermissionError, FileNotFoundError):
                    pass
        return hasher.hexdigest()

    def sync_files(self):
        if not os.path.exists(SOURCE_DIR):
            log.warning(f"Source not found: {SOURCE_DIR}")
            return False

        try:
            # Just do a full mirror copy, ignoring locked files
            if not os.path.exists(DEST_DIR):
                os.makedirs(DEST_DIR, exist_ok=True)
                
            copied = 0
            # Track dest files to remove ones that no longer exist in source
            valid_dest_files = set()
            
            for root, _, files in os.walk(SOURCE_DIR):
                rel_path = os.path.relpath(root, SOURCE_DIR)
                dest_root = os.path.join(DEST_DIR, rel_path) if rel_path != '.' else DEST_DIR
                os.makedirs(dest_root, exist_ok=True)
                
                for file in files:
                    src_file = os.path.join(root, file)
                    dst_file = os.path.join(dest_root, file)
                    valid_dest_files.add(dst_file)
                    
                    try:
                        # Copy if missing or modified
                        if not os.path.exists(dst_file) or os.stat(src_file).st_mtime > os.stat(dst_file).st_mtime:
                            shutil.copy2(src_file, dst_file)
                            copied += 1
                    except (PermissionError, FileNotFoundError):
                        pass # Skip locked files this tick
            
            # Remove deleted files from DEST_DIR
            for root, _, files in os.walk(DEST_DIR):
                for file in files:
                    dst_file = os.path.join(root, file)
                    if dst_file not in valid_dest_files:
                        try:
                            os.remove(dst_file)
                        except (PermissionError, FileNotFoundError):
                            pass
            return True
        except Exception as e:
            log.error(f"Sync failed: {e}")
            return False

    def enhance_audio(self, input_file: str, output_file: str) -> bool:
        # 1. Probe for CoverPath from TXXX frame
        cover_path = None
        try:
            probe_cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", input_file]
            _si = subprocess.STARTUPINFO()
            _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            result = subprocess.run(
                probe_cmd, capture_output=True, text=True, timeout=5,
                startupinfo=_si, creationflags=0x08000000  # CREATE_NO_WINDOW
            )
            if result.returncode == 0:
                import json
                probe_data = json.loads(result.stdout)
                tags = probe_data.get('format', {}).get('tags', {})
                # ID3v2 TXXX frames are often exposed as standard tags in ffprobe, sometimes as title-cased keys
                # We'll check variations of 'CoverPath'
                for k, v in tags.items():
                    if k.lower() == 'coverpath':
                        cover_path = v
                        break
        except Exception as e:
            log.warning(f"Failed to probe {input_file} for cover: {e}")

        command = [
            "ffmpeg", "-y", "-i", input_file
        ]
        
        has_cover = False
        if cover_path and os.path.exists(cover_path):
            command.extend(["-i", cover_path])
            has_cover = True
            log.info(f"Injecting cover: {cover_path}")
        elif cover_path:
            log.warning(f"Cover specified but not found: {cover_path}")

        command.extend([
            "-map_metadata", "0",
        ])
        
        if has_cover:
            # Map audio from first input, video (cover) from second input
            command.extend([
                "-map", "0:a",
                "-map", "1:v",
                "-c:v", "copy",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)"
            ])

        command.extend([
            "-af", "loudnorm=I=-12:TP=-1.0:LRA=11",
            "-c:a", "libmp3lame", "-b:a", "320k",
            "-id3v2_version", "3",
            output_file
        ])

        try:
            startupinfo = None
            creationflags = 0
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                creationflags = 0x00000080 
                
            process = subprocess.Popen(
                command, 
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL, 
                startupinfo=startupinfo,
                creationflags=creationflags
            )
            with self.process_lock:
                self.active_processes[input_file] = process
            process.wait()
            with self.process_lock:
                if input_file in self.active_processes:
                    del self.active_processes[input_file]
            return process.returncode == 0
        except Exception as e:
            log.error(f"Execution engine failed: {e}")
            return False

    def move_music(self):
        if not os.path.exists(BROWSER_DIR): return
        os.makedirs(MUSIC_DEST, exist_ok=True)
            
        targets = []
        try:
            for file in os.listdir(BROWSER_DIR):
                if not file.lower().endswith(".mp3") or file.lower().endswith(".tmp.mp3"):
                    continue
                src = os.path.join(BROWSER_DIR, file)
                if src in self.processing_files:
                    continue
                try:
                    # Quick lock check to ensure file is fully downloaded
                    os.rename(src, src)
                    targets.append(file)
                except PermissionError:
                    continue
        except Exception:
            return

        if not targets:
            return

        # TARGETS ACQUIRED: Claim absolute priority via lock file
        try:
            with open(PRIORITY_LOCK, 'w') as f: f.write("1")
            log.info("Priority lock deployed. Waiting 3 seconds for CPU Governor to suspend video...")
            # Give GOV.ps1 time to read the lock and freeze video operations
            time.sleep(3)
        except Exception: 
            pass

        try:
            for file in targets:
                src = os.path.join(BROWSER_DIR, file)
                self.processing_files.add(src)
                temp_src = src + ".tmp.mp3"
                
                log.info(f"Processing audio asset (High Priority Mode): {file}")
                
                if self.enhance_audio(src, temp_src):
                    try:
                        os.replace(temp_src, src)
                        dst = os.path.join(MUSIC_DEST, file)
                        if os.path.exists(dst):
                            name, ext = os.path.splitext(file)
                            dst = os.path.join(MUSIC_DEST, f"{name}_{int(time.time())}{ext}")
                        shutil.move(src, dst)
                        log.info(f"Relocated final audio: {os.path.basename(dst)}")
                    except Exception as e:
                        log.error(f"Filesystem transaction failed: {e}")
                        if os.path.exists(temp_src):
                            try: os.remove(temp_src)
                            except Exception: pass
                else:
                    if os.path.exists(temp_src):
                        try: os.remove(temp_src)
                        except Exception: pass
                
                self.processing_files.discard(src)
        finally:
            # RELEASE PRIORITY
            try:
                if os.path.exists(PRIORITY_LOCK): 
                    os.remove(PRIORITY_LOCK)
                log.info("Priority lock released. Governor will restore background processing.")
            except Exception: 
                pass

    def check_backup(self):
        current_hash = self.hash_directory(SOURCE_DIR)
        
        # If this is the first run (last_hash is None), just sync
        if self.last_hash is None:
            if current_hash:
                log.info("First run - initial copy...")
                log.info(f"Syncing -> {DEST_NAME} ...")
                self.sync_files()
                self.last_hash = current_hash
                log.info("Initial copy done. Watching...")
            return

        if current_hash != self.last_hash and current_hash != "":
            log.info("Changes detected! Creating incremented backup...")
            # Create a backup of the current DEST_DIR before syncing the new changes
            if os.path.exists(DEST_DIR):
                # Roll over to 1 if we exceed 50
                if self.backup_counter > 50:
                    self.backup_counter = 1
                
                backup_name = f"{DEST_NAME}_{self.backup_counter}"
                backup_path = os.path.join(DEST_PARENT, backup_name)
                
                try:
                    # Remove the old one if it exists to overwrite
                    if os.path.exists(backup_path):
                        shutil.rmtree(backup_path, ignore_errors=True)
                    
                    shutil.copytree(DEST_DIR, backup_path)
                    log.info(f"Archived backup created: {backup_name}")
                    self.backup_counter += 1
                except Exception as e:
                    log.error(f"Failed to create backup {backup_name}: {e}")

            # Now sync the actual changes
            if self.sync_files():
                log.info(f"Sync complete: {DEST_NAME}")
                self.last_hash = current_hash

    def run(self):
        log.info("="*56)
        log.info(f"BZ BACKUP MONITOR v2.1 | vacuum34 (Upgraded)")
        log.info(f"PID      : {os.getpid()}")
        log.info(f"Source   : {SOURCE_DIR}")
        log.info(f"Dest     : {DEST_DIR}")
        log.info(f"Interval : {CHECK_INTERVAL}s  MaxBackups: 50")
        log.info("="*56)
        log.info("Monitor loop started.")
        
        while not self.stop_event.is_set():
            self.check_backup()
            self.move_music()
            self.stop_event.wait(CHECK_INTERVAL)

def run_tray(monitor):
    try:
        import pystray
        from PIL import Image, ImageDraw
        
        def create_image():
            # Create a simple icon if no external icon exists
            image = Image.new('RGB', (64, 64), color=(30, 30, 30))
            dc = ImageDraw.Draw(image)
            dc.ellipse((10, 10, 54, 54), fill=(0, 150, 255))
            return image
            
        def on_quit(icon, item):
            monitor.stop()
            icon.stop()
            os._exit(0)
            
        icon = pystray.Icon("bz_monitor", create_image(), "BZ & Audio Priority", menu=pystray.Menu(
            pystray.MenuItem("Exit", on_quit)
        ))
        icon.run()
    except ImportError:
        log.warning("pystray or pillow not installed. Tray icon disabled.")

def main():
    if not all((SOURCE_DIR, DEST_PARENT, BROWSER_DIR, MUSIC_DEST)):
        raise SystemExit("Configure SAITULS_BACKUP_SOURCE, SAITULS_BACKUP_DEST_PARENT, "
                         "SAITULS_BACKUP_BROWSER and SAITULS_BACKUP_MUSIC first.")
    monitor = BackupMonitor()
    
    def sig_handler(sig, frame):
        monitor.stop()
        sys.exit(0)
        
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    
    # Start monitor loop in background thread
    t = threading.Thread(target=monitor.run, daemon=True)
    t.start()
    
    # Run tray icon on main thread (required for Windows GUI message pump)
    try:
        run_tray(monitor)
    except KeyboardInterrupt:
        monitor.stop()
            
if __name__ == '__main__':
    main()
