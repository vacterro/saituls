"""
queue_core.py - Direct SQLite backend for OpenCode native queue management.
Provides thread-safe, transactional inspection and mutation of session_input.
Part of SAIPATCH / SAITULS.
"""
import os
import sys
import json
import time
import sqlite3
import secrets
from typing import List, Dict, Optional, Tuple, Any, Set

try:
    import psutil
except ImportError:
    psutil = None


def find_db_path() -> str:
    env_path = os.environ.get("OPENCODE_DB")
    if env_path and os.path.exists(env_path):
        return env_path
    
    # 1. ~/.local/share/opencode/opencode.db (OpenCode standard XDG path on Windows)
    p1 = os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db")
    if os.path.exists(p1):
        return p1
        
    # 2. %LOCALAPPDATA%/opencode/opencode.db
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        p2 = os.path.join(local_app_data, "opencode", "opencode.db")
        if os.path.exists(p2):
            return p2
            
    return p1


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or find_db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"OpenCode database not found at: {path}")
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_live_opencode_directories() -> Set[str]:
    """Scans running processes to find which directories have an active OpenCode process."""
    dirs = set()
    if not psutil:
        return dirs
    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            name = proc.info.get('name')
            if name and 'opencode' in name.lower():
                cmd = proc.info.get('cmdline') or []
                for arg in cmd:
                    if isinstance(arg, str) and os.path.isdir(arg):
                        dirs.add(os.path.normpath(arg).lower())
                try:
                    cwd = proc.cwd()
                    if cwd and os.path.isdir(cwd):
                        dirs.add(os.path.normpath(cwd).lower())
                except Exception:
                    pass
        except Exception:
            pass
    return dirs


def list_recent_sessions(limit: int = 35, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, directory, title, time_updated
            FROM session
            ORDER BY time_updated DESC
            LIMIT ?
            """,
            (limit,)
        )
        sessions = []
        for r in c.fetchall():
            d = r[1] or ""
            base = os.path.basename(d.replace("\\", "/").rstrip("/")) or d
            sessions.append({
                "id": r[0],
                "directory": d,
                "project_name": base,
                "title": r[2] or "",
                "time_updated": r[3]
            })
        return sessions
    finally:
        conn.close()


def get_active_session(db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    sessions = list_recent_sessions(limit=1, db_path=db_path)
    return sessions[0] if sessions else None


def get_session_status(session_id: str, db_path: Optional[str] = None, live_dirs: Optional[Set[str]] = None, check_live_process: bool = True) -> Dict[str, Any]:
    conn = get_connection(db_path)
    try:
        c = conn.cursor()
        # Fetch session directory
        c.execute("SELECT directory FROM session WHERE id = ?", (session_id,))
        s_row = c.fetchone()
        s_dir = os.path.normpath(s_row[0]).lower() if s_row and s_row[0] else ""

        # Check if an OpenCode process is actively running in this directory
        if check_live_process and psutil:
            if live_dirs is None:
                live_dirs = get_live_opencode_directories()
            is_proc_live = bool(s_dir and s_dir in live_dirs)
        else:
            is_proc_live = True

        # Last promoted turn
        c.execute(
            """
            SELECT id, prompt, admitted_seq, promoted_seq, time_created
            FROM session_input
            WHERE session_id = ? AND promoted_seq IS NOT NULL
            ORDER BY promoted_seq DESC
            LIMIT 1
            """,
            (session_id,)
        )
        promoted_row = c.fetchone()
        promoted_info = None
        if promoted_row:
            try:
                p_data = json.loads(promoted_row[1])
                text = p_data.get("text", "")
            except Exception:
                text = promoted_row[1]
            promoted_info = {
                "id": promoted_row[0],
                "text": text,
                "admitted_seq": promoted_row[2],
                "promoted_seq": promoted_row[3],
                "time_created": promoted_row[4]
            }

        # Last event
        c.execute(
            """
            SELECT seq, type, data
            FROM event
            WHERE aggregate_id = ?
            ORDER BY seq DESC
            LIMIT 1
            """,
            (session_id,)
        )
        event_row = c.fetchone()
        state = "IDLE"
        detail = ""

        if not is_proc_live and psutil:
            # If no OpenCode process is open in this directory, it is unequivocally IDLE
            state = "IDLE"
            detail = "No active process"
            if event_row:
                seq, ev_type, data_raw = event_row
                if "step.failed" in ev_type:
                    detail = f"Last turn failed at step {seq}"
        elif event_row:
            seq, ev_type, data_raw = event_row
            data = {}
            if data_raw:
                try:
                    data = json.loads(data_raw)
                except Exception:
                    pass
            finish = data.get("finish")
            tool_name = data.get("tool") or (data.get("input", {}).get("name") if isinstance(data.get("input"), dict) else None)
            err_msg = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else None

            if "step.failed" in ev_type:
                state = "FAILED"
                detail = f"Step {seq} failed: {err_msg or 'Error'}"
            elif ev_type.endswith(".step.ended.2"):
                if finish == "stop":
                    state = "IDLE"
                    detail = f"Step {seq} completed"
                elif finish == "tool-calls":
                    state = "RUNNING"
                    detail = f"Step {seq}: Tool calls pending response"
                else:
                    state = "RUNNING"
                    detail = f"Step {seq} ({finish or 'in progress'})"
            elif "tool.called" in ev_type:
                state = "RUNNING"
                detail = f"Step {seq}: Tool '{tool_name}' executing"
            elif "reasoning.started" in ev_type:
                state = "RUNNING"
                detail = f"Step {seq}: Reasoning / thinking"
            elif "step.started" in ev_type:
                state = "RUNNING"
                detail = f"Step {seq}: Step in progress"
            elif "permission.asked" in ev_type or "question.asked" in ev_type:
                state = "NEEDS_HUMAN"
                detail = f"Step {seq}: Waiting for human input"
            else:
                state = "IDLE"
                detail = f"Step {seq}: Idle"

        return {
            "session_id": session_id,
            "is_process_live": is_proc_live,
            "state": state,
            "detail": detail,
            "active_turn": promoted_info
        }
    finally:
        conn.close()


def get_pending_queue(session_id: str, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, prompt, delivery, admitted_seq, time_created
            FROM session_input
            WHERE session_id = ? AND promoted_seq IS NULL AND delivery = 'queue'
            ORDER BY admitted_seq ASC
            """,
            (session_id,)
        )
        queue = []
        for r in c.fetchall():
            text = ""
            try:
                p_data = json.loads(r[1])
                text = p_data.get("text", "")
            except Exception:
                text = r[1]
            queue.append({
                "id": r[0],
                "session_id": session_id,
                "text": text,
                "delivery": r[2],
                "admitted_seq": r[3],
                "time_created": r[4]
            })
        return queue
    finally:
        conn.close()


def get_sent_history(session_id: str, limit: int = 50, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns already sent/promoted prompt history for a session."""
    conn = get_connection(db_path)
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, prompt, delivery, admitted_seq, promoted_seq, time_created
            FROM session_input
            WHERE session_id = ? AND promoted_seq IS NOT NULL
            ORDER BY promoted_seq DESC
            LIMIT ?
            """,
            (session_id, limit)
        )
        history = []
        for r in c.fetchall():
            text = ""
            try:
                p_data = json.loads(r[1])
                text = p_data.get("text", "")
            except Exception:
                text = r[1]
            history.append({
                "id": r[0],
                "session_id": session_id,
                "text": text,
                "delivery": r[2],
                "admitted_seq": r[3],
                "promoted_seq": r[4],
                "time_created": r[5]
            })
        return history
    finally:
        conn.close()


def get_all_pending_queues(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT
                si.id,
                si.session_id,
                s.directory,
                s.title,
                si.prompt,
                si.delivery,
                si.admitted_seq,
                si.time_created
            FROM session_input si
            JOIN session s ON s.id = si.session_id
            WHERE si.promoted_seq IS NULL AND si.delivery = 'queue'
            ORDER BY si.time_created ASC
            """
        )
        queue = []
        for r in c.fetchall():
            text = ""
            try:
                p_data = json.loads(r[4])
                text = p_data.get("text", "")
            except Exception:
                text = r[4]
            d = r[2] or ""
            base = os.path.basename(d.replace("\\", "/").rstrip("/")) or d
            queue.append({
                "id": r[0],
                "session_id": r[1],
                "directory": d,
                "project_name": base,
                "session_title": r[3] or "",
                "text": text,
                "delivery": r[5],
                "admitted_seq": r[6],
                "time_created": r[7]
            })
        return queue
    finally:
        conn.close()


def get_all_sent_history(limit: int = 50, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT
                si.id,
                si.session_id,
                s.directory,
                s.title,
                si.prompt,
                si.delivery,
                si.admitted_seq,
                si.promoted_seq,
                si.time_created
            FROM session_input si
            JOIN session s ON s.id = si.session_id
            WHERE si.promoted_seq IS NOT NULL
            ORDER BY si.promoted_seq DESC
            LIMIT ?
            """,
            (limit,)
        )
        history = []
        for r in c.fetchall():
            text = ""
            try:
                p_data = json.loads(r[4])
                text = p_data.get("text", "")
            except Exception:
                text = r[4]
            d = r[2] or ""
            base = os.path.basename(d.replace("\\", "/").rstrip("/")) or d
            history.append({
                "id": r[0],
                "session_id": r[1],
                "directory": d,
                "project_name": base,
                "session_title": r[3] or "",
                "text": text,
                "delivery": r[5],
                "admitted_seq": r[6],
                "promoted_seq": r[7],
                "time_created": r[8]
            })
        return history
    finally:
        conn.close()


def get_sessions_summary(limit: int = 35, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT id, directory, title, time_updated
            FROM session
            ORDER BY time_updated DESC
            LIMIT ?
            """,
            (limit,)
        )
        raw_sessions = c.fetchall()
        
        # Batch count pending items per session
        c.execute(
            """
            SELECT session_id, count(*)
            FROM session_input
            WHERE promoted_seq IS NULL AND delivery = 'queue'
            GROUP BY session_id
            """
        )
        counts = dict(c.fetchall())

        live_dirs = get_live_opencode_directories()
        
        summaries = []
        for r in raw_sessions:
            s_id = r[0]
            d = r[1] or ""
            base = os.path.basename(d.replace("\\", "/").rstrip("/")) or d
            pending_count = counts.get(s_id, 0)
            
            status = get_session_status(s_id, db_path=db_path, live_dirs=live_dirs)
            
            summaries.append({
                "id": s_id,
                "directory": d,
                "project_name": base,
                "title": r[2] or "",
                "time_updated": r[3],
                "pending_count": pending_count,
                "is_process_live": status["is_process_live"],
                "state": status["state"],
                "detail": status["detail"],
                "active_turn": status["active_turn"]
            })
        return summaries
    finally:
        conn.close()


def delete_queued_item(item_id: str, db_path: Optional[str] = None) -> bool:
    conn = get_connection(db_path)
    try:
        with conn:
            c = conn.cursor()
            c.execute("DELETE FROM session_input WHERE id = ? AND promoted_seq IS NULL", (item_id,))
            return c.rowcount > 0
    finally:
        conn.close()


def clear_queue(session_id: str, db_path: Optional[str] = None) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            c = conn.cursor()
            c.execute("DELETE FROM session_input WHERE session_id = ? AND promoted_seq IS NULL", (session_id,))
            return c.rowcount
    finally:
        conn.close()


def clear_all_queues(db_path: Optional[str] = None) -> int:
    conn = get_connection(db_path)
    try:
        with conn:
            c = conn.cursor()
            c.execute("DELETE FROM session_input WHERE promoted_seq IS NULL AND delivery = 'queue'")
            return c.rowcount
    finally:
        conn.close()


def delete_session(session_id: str, db_path: Optional[str] = None) -> bool:
    """Deletes a session and all its cascading records from the database."""
    conn = get_connection(db_path)
    try:
        with conn:
            c = conn.cursor()
            for tbl in ["event", "session_input", "session_message", "part"]:
                try:
                    col = "aggregate_id" if tbl == "event" else "session_id"
                    c.execute(f"DELETE FROM {tbl} WHERE {col} = ?", (session_id,))
                except sqlite3.OperationalError:
                    pass
            c.execute("DELETE FROM session WHERE id = ?", (session_id,))
            return c.rowcount > 0
    finally:
        conn.close()


def edit_queued_prompt(item_id: str, new_text: str, db_path: Optional[str] = None) -> bool:
    conn = get_connection(db_path)
    try:
        with conn:
            c = conn.cursor()
            c.execute("SELECT prompt FROM session_input WHERE id = ? AND promoted_seq IS NULL", (item_id,))
            row = c.fetchone()
            if not row:
                return False
            try:
                p_data = json.loads(row[0])
            except Exception:
                p_data = {}
            p_data["text"] = new_text
            new_prompt_json = json.dumps(p_data, separators=(",", ":"))
            c.execute("UPDATE session_input SET prompt = ? WHERE id = ? AND promoted_seq IS NULL", (new_prompt_json, item_id))
            return c.rowcount > 0
    finally:
        conn.close()


def swap_items(item_id_1: str, item_id_2: str, db_path: Optional[str] = None) -> bool:
    conn = get_connection(db_path)
    try:
        with conn:
            c = conn.cursor()
            c.execute("SELECT id, session_id, admitted_seq FROM session_input WHERE id = ? AND promoted_seq IS NULL", (item_id_1,))
            r1 = c.fetchone()
            c.execute("SELECT id, session_id, admitted_seq FROM session_input WHERE id = ? AND promoted_seq IS NULL", (item_id_2,))
            r2 = c.fetchone()
            if not r1 or not r2:
                return False
            if r1[1] != r2[1]:
                return False
            
            id1, s_id, seq1 = r1
            id2, _, seq2 = r2
            
            c.execute("UPDATE session_input SET admitted_seq = -999999 WHERE id = ?", (id1,))
            c.execute("UPDATE session_input SET admitted_seq = ? WHERE id = ?", (seq1, id2))
            c.execute("UPDATE session_input SET admitted_seq = ? WHERE id = ?", (seq2, id1))
            return True
    finally:
        conn.close()


def move_item_in_queue(session_id: str, item_id: str, direction: str, db_path: Optional[str] = None) -> bool:
    queue = get_pending_queue(session_id, db_path=db_path)
    idx = -1
    for i, item in enumerate(queue):
        if item["id"] == item_id:
            idx = i
            break
    if idx == -1:
        return False
    
    if direction == "up" and idx > 0:
        return swap_items(item_id, queue[idx - 1]["id"], db_path=db_path)
    elif direction == "down" and idx < len(queue) - 1:
        return swap_items(item_id, queue[idx + 1]["id"], db_path=db_path)
    return False


def add_queued_prompt(session_id: str, text: str, db_path: Optional[str] = None) -> Optional[str]:
    conn = get_connection(db_path)
    try:
        with conn:
            c = conn.cursor()
            c.execute("SELECT COALESCE(MAX(admitted_seq), 0) FROM session_input WHERE session_id = ?", (session_id,))
            max_in = c.fetchone()[0]
            
            c.execute("SELECT COALESCE(MAX(seq), 0) FROM event WHERE aggregate_id = ?", (session_id,))
            max_ev = c.fetchone()[0]
            
            next_seq = max(max_in, max_ev) + 1
            
            msg_id = f"msg_{secrets.token_hex(7)}{secrets.token_urlsafe(9)[:12]}"
            prompt_obj = {"text": text}
            prompt_json = json.dumps(prompt_obj, separators=(",", ":"))
            now_ms = int(time.time() * 1000)
            
            c.execute(
                """
                INSERT INTO session_input (id, session_id, prompt, delivery, admitted_seq, promoted_seq, time_created)
                VALUES (?, ?, ?, 'queue', ?, NULL, ?)
                """,
                (msg_id, session_id, prompt_json, next_seq, now_ms)
            )
            return msg_id
    finally:
        conn.close()
