"""
viewer_observer.py - Read-only SQLite observer for Queue Viewer Cockpit.
Lives in a QThread worker. Opens SQLite in mode=ro with query_only.
Emits signals with immutable data snapshots. Zero writes. Zero UI-thread I/O.

V1 threading contract: the GUI NEVER calls worker methods directly. All
cross-thread control flows through the queued signals select_session(),
apply_process_snapshots() and stop_worker(), connected after moveToThread.
Timers, the SQLite connection and all DB I/O are created/owned/closed in the
worker's own thread.
"""
import os
import json
import time
import threading
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Set, Tuple

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

import viewer_presentation as present


@dataclass
class SessionSummary:
    id: str
    directory: str
    title: str
    project_name: str
    time_updated: int
    pending_count: int
    state: str  # RUNNING, IDLE, FAILED, NEEDS_HUMAN
    detail: str
    is_process_live: bool
    latest_event_seq: int


@dataclass
class LiveEvent:
    seq: int
    event_type: str   # normalized (no trailing .N)
    raw_type: str
    data: Dict[str, Any]


@dataclass
class PendingItem:
    id: str
    prompt_text: str
    admitted_seq: int
    time_created: int


def normalize_event_type(raw: str) -> str:
    """Strip trailing version suffix: 'session.next.step.ended.2' -> 'session.next.step.ended'"""
    parts = raw.rsplit(".", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return raw


def utf8_safe_truncate(text: str, max_bytes: int, marker: str) -> str:
    """Truncate text to fit within max_bytes of UTF-8 INCLUDING the marker.

    Never splits an encoded character: the result is always a valid UTF-8
    string whose encoded size is <= max_bytes.
    """
    marker_bytes = len(marker.encode("utf-8"))
    budget = max_bytes - marker_bytes
    if budget < 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    cut = encoded[:budget]
    # Back off to the start of the last complete UTF-8 sequence.
    while cut:
        try:
            return cut.decode("utf-8") + marker
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "" + marker


def native_event_time(data: Dict[str, Any]) -> Optional[float]:
    """Native event timestamp in SECONDS from a durable/live event payload.

    Recorded real OpenCode events carry data.timestamp in MILLISECONDS
    (e.g. 1788644241181); the 2.5.2 projector also tolerates ISO-8601
    strings. Numeric ms -> seconds, ISO string -> parsed, otherwise None
    (caller falls back to observation time).
    """
    ts = data.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        if ts <= 0:
            return None
        return ts / 1000.0 if ts > 1e11 else float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime, timezone
            s = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


class ObserverSignals(QObject):
    """Signals emitted by ReadOnlyObserver. Cross-thread safe via Qt signal system."""
    sidebar_updated = pyqtSignal(list)       # List[SessionSummary]
    pending_updated = pyqtSignal(str, list)  # (session_id, List[PendingItem])
    events_received = pyqtSignal(str, list)  # (session_id, List[LiveEvent])
    # Target A1: the durable text handoff is an IMMUTABLE snapshot captured
    # atomically at emission time (session, assistantMessageID, ordered
    # parts with textID/finality, durable seq witness). The GUI NEVER
    # reconstructs identity from mutable worker internals.
    live_text_updated = pyqtSignal(str, object)  # (session_id, DurableTextSnapshot)
    activity_changed = pyqtSignal(str, str)  # (session_id, compact_activity_line)
    event_observed = pyqtSignal(str, int, float)  # (session_id, seq, timestamp) EVERY new durable event
    observer_health = pyqtSignal(str, float) # ('LIVE'|'BUSY'|'ERROR', last_success_age_sec)
    error = pyqtSignal(str)


class ReadOnlyObserver(QObject):
    """Read-only SQLite observer. Lives in a worker QThread.

    Invariants:
    - Zero INSERT/UPDATE/DELETE
    - mode=ro URI connection
    - PRAGMA query_only = ON
    - busy_timeout effectively <=50ms
    - Skip refresh if previous still running
    - Busy/locked -> skip, don't wait
    - Every durable event emits event_observed (including text.delta)
    - pending_updated ALWAYS emits for the selected session, even with []
    """

    MAX_TEXT_BYTES = 128 * 1024  # 128 KiB
    TRUNCATION_MARKER = "\n\n[... output truncated at 128 KiB ...]"

    # Queued command signals (GUI -> worker). Connected AFTER moveToThread.
    select_session = pyqtSignal(str)
    apply_snapshots = pyqtSignal(object)
    refresh_now = pyqtSignal()
    stop_worker = pyqtSignal()

    def __init__(self, db_path: str):
        super().__init__()
        self.signals = ObserverSignals()
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

        # Selected session tracking
        self._selected_session_id: Optional[str] = None
        self._last_event_seq: int = 0
        self._last_activity: str = ""

        # Current-turn multi-part text model:
        #   current assistant message ID, ordered text parts, active part ID.
        # ONE total UTF-8 byte bound is enforced across the WHOLE turn, not
        # per part (a turn with many parts must never exceed 128 KiB).
        self._current_message_id: Optional[str] = None
        self._text_parts: List[Dict[str, Any]] = []  # {id, chunks:[], bytes:int}
        self._active_text_part_id: Optional[str] = None
        self._turn_bytes: int = 0
        self._turn_truncated: bool = False

        # Execution state machine per session (bounded, in-memory only).
        self._session_states: Dict[str, str] = {}

        # Verified process snapshots (injected via queued signal from resolver).
        self._live_snapshots: Tuple[Tuple, ...] = ()

        # Refresh guards (prevent overlapping refreshes)
        self._sidebar_running: bool = False
        self._events_running: bool = False

        # Runtime thread-ownership proof (DEBUG/test assertions). Recorded on
        # every DB / selection / connection operation so tests can prove the
        # work never executes on the QApplication GUI thread.
        self._last_refresh_thread_id = None
        self._last_select_thread_id = None
        self._conn_open_thread_id = None
        self._last_open_uri: str = ""

        # Health tracking
        self._last_success_time: float = 0.0

        # Timers created in start() after moveToThread
        self._event_timer: Optional[QTimer] = None
        self._sidebar_timer: Optional[QTimer] = None

    # ── Queued worker commands (GUI emits these, never calls methods) ──

    @pyqtSlot(str)
    def _on_select_session(self, session_id: str):
        """Queued selection command. Empty string clears the target."""
        self.set_selected_session(session_id or None)

    @pyqtSlot(object)
    def _on_snapshots(self, snapshots):
        """Queued snapshot command from the endpoint resolver worker."""
        self._live_snapshots = tuple(snapshots or ())

    @pyqtSlot()
    def _on_refresh_now(self):
        """F5: immediate sidebar + event refresh in the worker thread."""
        self._refresh_sidebar()
        self._refresh_events()

    @pyqtSlot()
    def _on_stop(self):
        """Queued stop: stop own timers, close own connection, in own thread."""
        if self._event_timer:
            self._event_timer.stop()
            self._event_timer = None
        if self._sidebar_timer:
            self._sidebar_timer.stop()
            self._sidebar_timer = None
        if self._conn:
            try:
                self._conn.close()
            except sqlite3.Error:
                # A cross-thread close attempt would raise ProgrammingError;
                # stop() runs in the owner thread, so this must never happen.
                raise
            self._conn = None

    # Compatibility direct-call entry points (worker-thread / tests only).
    def set_selected_session(self, session_id: Optional[str]) -> None:
        """Set the session to track for live events. Resets text state and triggers hydration."""
        self._last_select_thread_id = threading.get_ident()
        if session_id != self._selected_session_id:
            self._last_event_seq = 0
            self._reset_turn()
        self._selected_session_id = session_id

    def set_live_directories(self, dirs: Set[str]) -> None:
        """Compatibility shim (directory heuristic NO LONGER used for liveness)."""
        # Liveness authority is exclusively the verified process snapshots.
        pass

    # ── Connection Management ───────────────────────────────────────

    def _open_connection(self) -> Optional[sqlite3.Connection]:
        """Open read-only SQLite connection. Returns None on failure."""
        if self._conn:
            try:
                self._conn.execute("SELECT 1")
                return self._conn
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

        if not os.path.isfile(self._db_path):
            self.signals.observer_health.emit("ERROR", 0.0)
            self.signals.error.emit(f"Database not found: {self._db_path}")
            return None

        try:
            uri = f"file:{self._db_path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=0.05)
            self._conn.execute("PRAGMA query_only = ON")
            self._last_open_uri = uri
            self._conn_open_thread_id = threading.get_ident()
            self._last_success_time = time.time()
            return self._conn
        except Exception as e:
            self.signals.observer_health.emit("ERROR", 0.0)
            self.signals.error.emit(f"DB open failed: {e}")
            return None

    # ── Verified process liveness (snapshot authority) ──────────────

    def _verified_live_session_ids(self) -> Set[str]:
        """Session IDs owned by a VERIFIED live endpoint snapshot.

        Liveness is derived ONLY from snapshots: pid + start_time +
        verified executable path + exact session ID membership. Directory
        basename heuristics are never used.
        """
        live: Set[str] = set()
        for snap in self._live_snapshots:
            # snap is a ProcessSnapshot (or equivalent tuple-backed record)
            try:
                live.update(snap.verified_session_ids)
            except AttributeError:
                # tuple form: (pid, start_time, exe, endpoint, sessions)
                if len(snap) >= 5 and isinstance(snap[4], (list, tuple, set)):
                    live.update(snap[4])
        return live

    # ── Execution state machine ─────────────────────────────────────

    def apply_execution_event(self, session_id: str, norm_type: str,
                              data: Dict[str, Any], seq: int) -> None:
        """Advance the execution state machine from lifecycle-significant events.

        Separate from EVENT ACTIVITY: text/reasoning/tool events while a
        step is active keep RUNNING and never reset it to IDLE.
        """
        state = self._session_states.get(session_id, "IDLE")

        if "prompted" in norm_type:
            state = "RUNNING"
        elif "step.started" in norm_type:
            state = "RUNNING"
        elif "step.failed" in norm_type:
            state = "FAILED"
        elif "permission.asked" in norm_type or "question.asked" in norm_type:
            state = "NEEDS_HUMAN"
        elif norm_type.endswith("step.ended"):
            finish = data.get("finish")
            if finish == "tool-calls":
                state = "RUNNING"  # next step of the same generation follows
            else:
                # Terminal step (finish stop/abort/other): back to IDLE.
                state = "IDLE"
        # reasoning/text/tool activity while active: NO state change.
        # (text.delta must never flip RUNNING -> IDLE.)

        self._session_states[session_id] = state

    # ── Sidebar Refresh ─────────────────────────────────────────────

    def start(self) -> None:
        """Called after moveToThread. Open connection and start timers."""
        self._open_connection()

        self._event_timer = QTimer(self)
        self._event_timer.timeout.connect(self._refresh_events)
        self._event_timer.start(200)

        self._sidebar_timer = QTimer(self)
        self._sidebar_timer.timeout.connect(self._refresh_sidebar)
        self._sidebar_timer.start(1000)

        # Initial sidebar refresh
        self._refresh_sidebar()

    def stop(self) -> None:
        """Direct-call stop (worker thread only). Prefer _on_stop via signal."""
        self._on_stop()

    def _refresh_sidebar(self) -> None:
        """Batched sidebar refresh. One read transaction for all sessions."""
        if self._sidebar_running:
            return
        self._sidebar_running = True
        self._last_refresh_thread_id = threading.get_ident()
        try:
            conn = self._open_connection()
            if not conn:
                return

            c = conn.cursor()

            # Query 1: Sessions with pending counts (batched)
            c.execute("""
                SELECT s.id, s.directory, s.title, s.time_updated,
                       COALESCE(q.cnt, 0) as pending_count
                FROM session s
                LEFT JOIN (
                    SELECT session_id, COUNT(*) as cnt
                    FROM session_input
                    WHERE promoted_seq IS NULL AND delivery = 'queue'
                    GROUP BY session_id
                ) q ON q.session_id = s.id
                ORDER BY s.time_updated DESC
                LIMIT 35
            """)
            session_rows = c.fetchall()

            # Pending items for selected session — ALWAYS emitted, even [].
            pending_items: List[PendingItem] = []
            if self._selected_session_id:
                c.execute("""
                    SELECT id, prompt, admitted_seq, time_created
                    FROM session_input
                    WHERE session_id = ? AND promoted_seq IS NULL AND delivery = 'queue'
                    ORDER BY admitted_seq ASC
                """, (self._selected_session_id,))
                for row in c.fetchall():
                    try:
                        pdata = json.loads(row[1])
                        text = pdata.get("text", row[1])
                    except (json.JSONDecodeError, TypeError):
                        text = row[1]
                    pending_items.append(PendingItem(
                        id=row[0], prompt_text=text,
                        admitted_seq=row[2], time_created=row[3]
                    ))

            if not session_rows:
                self.signals.sidebar_updated.emit([])
                if self._selected_session_id:
                    # N -> 0 (or empty DB): table must clear immediately.
                    self.signals.pending_updated.emit(self._selected_session_id, [])
                self._last_success_time = time.time()
                self.signals.observer_health.emit("LIVE", 0.0)
                return

            session_ids = [r[0] for r in session_rows]

            # Query 2: latest K lifecycle events per session (batched, bounded).
            # Hydrate the state machine without N+1 history scans.
            placeholders = ",".join("?" * len(session_ids))
            c.execute(f"""
                SELECT aggregate_id, seq, type, data FROM (
                    SELECT e.aggregate_id, e.seq, e.type, e.data,
                           ROW_NUMBER() OVER (
                               PARTITION BY e.aggregate_id ORDER BY e.seq DESC
                           ) AS rn
                    FROM event e
                    WHERE e.aggregate_id IN ({placeholders})
                ) WHERE rn <= 24
            """, session_ids)
            events_by_session: Dict[str, List[tuple]] = {}
            latest_seq: Dict[str, int] = {}
            for aggregate_id, seq, ev_type, data_raw in c.fetchall():
                events_by_session.setdefault(aggregate_id, []).append((seq, ev_type, data_raw))
                if aggregate_id not in latest_seq or seq > latest_seq[aggregate_id]:
                    latest_seq[aggregate_id] = seq

            # Build SessionSummary list
            live_sessions = self._verified_live_session_ids()
            summaries: List[SessionSummary] = []
            for sid, directory, title, time_updated, pending_count in session_rows:
                # State machine: hydrate from recent lifecycle events in
                # chronological order, then apply the newest.
                events = sorted(events_by_session.get(sid, []), key=lambda r: r[0])
                state = "IDLE"
                detail = ""
                for seq, ev_type, data_raw in events:
                    norm = normalize_event_type(ev_type)
                    edata = {}
                    if data_raw:
                        try:
                            edata = json.loads(data_raw)
                        except Exception:
                            pass
                    self.apply_execution_event(sid, norm, edata, seq)
                    detail = self._describe_event(norm, edata, seq)
                state = self._session_states.get(sid, "IDLE")

                # Live authority: only a verified endpoint snapshot owning
                # this exact session ID proves liveness. A stale RUNNING on
                # an unverified session is downgraded truthfully to IDLE.
                is_live = sid in live_sessions
                if not is_live and state == "RUNNING":
                    state = "IDLE"
                    detail = "No verified live endpoint for this session"

                project_name = os.path.basename(directory) if directory else title or sid[:12]

                summaries.append(SessionSummary(
                    id=sid,
                    directory=directory or "",
                    title=title or "",
                    project_name=project_name,
                    time_updated=time_updated,
                    pending_count=pending_count,
                    state=state,
                    detail=detail,
                    is_process_live=is_live,
                    latest_event_seq=latest_seq.get(sid, 0),
                ))

            self.signals.sidebar_updated.emit(summaries)

            # ALWAYS emit pending for the selected session (including [] so
            # the NEXT QUEUE table clears when the native queue drains to 0).
            if self._selected_session_id:
                self.signals.pending_updated.emit(self._selected_session_id, pending_items)

            self._last_success_time = time.time()
            self.signals.observer_health.emit("LIVE", 0.0)

        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                age = time.time() - self._last_success_time if self._last_success_time else 0
                self.signals.observer_health.emit("BUSY", age)
            else:
                self.signals.observer_health.emit("ERROR", 0.0)
                self.signals.error.emit(f"Sidebar: {e}")
        except Exception as e:
            self.signals.observer_health.emit("ERROR", 0.0)
            self.signals.error.emit(f"Sidebar: {e}")
        finally:
            self._sidebar_running = False

    @staticmethod
    def _describe_event(norm: str, edata: Dict[str, Any], seq: int) -> str:
        finish = edata.get("finish")
        tool_name = edata.get("tool") or ""
        err_obj = edata.get("error", {})
        err_msg = err_obj.get("message", "") if isinstance(err_obj, dict) else ""
        if "step.failed" in norm:
            return f"Step {seq} failed: {err_msg or 'Error'}"
        if norm.endswith("step.ended"):
            if finish == "stop":
                return f"Step {seq} completed"
            if finish == "tool-calls":
                return f"Step {seq}: Tool calls pending"
            return f"Step {seq} ({finish or 'in progress'})"
        if "tool.called" in norm:
            return f"Step {seq}: Tool '{tool_name}'"
        if "reasoning.started" in norm:
            return f"Step {seq}: Thinking"
        if "step.started" in norm:
            return f"Step {seq}: In progress"
        if "permission.asked" in norm or "question.asked" in norm:
            return f"Step {seq}: Waiting for input"
        if "prompted" in norm:
            return f"Step {seq}: Prompted"
        return f"Step {seq}"

    # ── Event Tail Refresh ──────────────────────────────────────────

    def _refresh_events(self) -> None:
        """Incremental event tail for selected session."""
        if self._events_running or not self._selected_session_id:
            return
        self._events_running = True
        self._last_refresh_thread_id = threading.get_ident()
        try:
            conn = self._open_connection()
            if not conn:
                return

            c = conn.cursor()
            sid = self._selected_session_id

            # Hydrate on first read for this session
            if self._last_event_seq == 0:
                self._hydrate(c, sid)
                return  # hydration handles signal emission

            # Incremental tail
            c.execute("""
                SELECT seq, type, data FROM event
                WHERE aggregate_id = ? AND seq > ?
                ORDER BY seq
            """, (sid, self._last_event_seq))
            rows = c.fetchall()

            if not rows:
                return

            events: List[LiveEvent] = []
            last_activity = self._last_activity

            for seq, raw_type, data_raw in rows:
                norm = normalize_event_type(raw_type)
                edata = {}
                if data_raw:
                    try:
                        edata = json.loads(data_raw)
                    except Exception:
                        pass

                events.append(LiveEvent(
                    seq=seq, event_type=norm, raw_type=raw_type, data=edata
                ))

                # Every newly consumed durable event feeds the age display,
                # including text.delta. Age uses the NATIVE event timestamp
                # when the row carries one (observer-read time is only the
                # fallback), so "Last event: 2s ago" describes event age,
                # not poll age.
                self.signals.event_observed.emit(
                    sid, seq, native_event_time(edata) or time.time())

                # Execution state machine
                self.apply_execution_event(sid, norm, edata, seq)

                # Text assembly
                last_activity = self._process_event_text(norm, edata)
                self._last_event_seq = seq

            self.signals.events_received.emit(sid, events)
            self.signals.live_text_updated.emit(
                sid, self._build_durable_snapshot())
            if last_activity != self._last_activity:
                self._last_activity = last_activity
                self.signals.activity_changed.emit(sid, last_activity)

        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                pass  # Skip this cycle, sidebar handles health
            else:
                self.signals.error.emit(f"Events: {e}")
        except Exception as e:
            self.signals.error.emit(f"Events: {e}")
        finally:
            self._events_running = False

    def _hydrate(self, cursor, session_id: str) -> None:
        """Reconstruct the current/last turn when viewer attaches late."""
        # Find latest prompted event (start of current generation)
        cursor.execute("""
            SELECT seq FROM event
            WHERE aggregate_id = ? AND type LIKE '%prompted%'
            ORDER BY seq DESC LIMIT 1
        """, (session_id,))
        row = cursor.fetchone()

        start_seq = 0
        if row:
            start_seq = row[0]
        else:
            # No prompted event — try last 50 events
            cursor.execute("""
                SELECT MIN(seq) FROM (
                    SELECT seq FROM event
                    WHERE aggregate_id = ?
                    ORDER BY seq DESC LIMIT 50
                )
            """, (session_id,))
            row = cursor.fetchone()
            if row and row[0]:
                start_seq = row[0]

        if start_seq == 0:
            self._last_event_seq = -1  # mark hydrated-with-no-events
            return

        # Fetch events from generation start
        cursor.execute("""
            SELECT seq, type, data FROM event
            WHERE aggregate_id = ? AND seq >= ?
            ORDER BY seq
        """, (session_id, start_seq))
        rows = cursor.fetchall()

        events: List[LiveEvent] = []
        last_activity = ""

        for seq, raw_type, data_raw in rows:
            norm = normalize_event_type(raw_type)
            edata = {}
            if data_raw:
                try:
                    edata = json.loads(data_raw)
                except Exception:
                    pass

            events.append(LiveEvent(
                seq=seq, event_type=norm, raw_type=raw_type, data=edata
            ))
            self.signals.event_observed.emit(
                session_id, seq, native_event_time(edata) or time.time())
            self.apply_execution_event(session_id, norm, edata, seq)
            activity = self._process_event_text(norm, edata)
            if activity:
                last_activity = activity
            self._last_event_seq = seq

        if events:
            self.signals.events_received.emit(session_id, events)
            self.signals.live_text_updated.emit(
                session_id, self._build_durable_snapshot())
            if last_activity:
                self._last_activity = last_activity
                self.signals.activity_changed.emit(session_id, last_activity)

    # ── Multi-part current-turn text model ──────────────────────────

    def _reset_turn(self) -> None:
        self._current_message_id = None
        self._text_parts = []
        self._active_text_part_id = None
        self._turn_bytes = 0
        self._turn_truncated = False

    # ── Target A1: immutable durable presentation snapshot ───────────

    def _build_durable_snapshot(self) -> "present.DurableTextSnapshot":
        """Capture an IMMUTABLE durable presentation snapshot ATOMICALLY at
        emission time (worker thread). The snapshot carries everything the
        GUI needs to reconcile the durable state — session, assistant
        message ID, ordered parts with textID + finality, and the durable
        event-sequence ordering witness — so the GUI NEVER reads mutable
        worker internals (e.g. _current_message_id) to reconstruct event
        identity after a queued-signal hop.
        """
        parts = tuple(
            present.DurableTextPart(
                text_id=p["id"], text="".join(p["chunks"]),
                finalized=bool(p.get("finalized")))
            for p in self._text_parts)
        return present.DurableTextSnapshot(
            session_id=self._selected_session_id or "",
            message_id=self._current_message_id,
            parts=parts,
            last_event_seq=self._last_event_seq)

    def _assembled_text(self) -> str:
        """Rendered LIVE OUTPUT: concatenation of the ordered text parts of
        the current turn. Reasoning content is never rendered. ONE total
        128 KiB UTF-8 bound across the whole turn (not per part)."""
        text = "".join("".join(p["chunks"]) for p in self._text_parts)
        if self._turn_truncated:
            text += self.TRUNCATION_MARKER
        return text

    def _enforce_total_bound(self, incoming_bytes: int = 0) -> bool:
        """Enforce ONE total UTF-8 bound across all parts of the turn.
        Trims the OLDEST text first (preserving the newest useful output),
        never splitting UTF-8 characters. Returns True when the turn ended
        up truncated."""
        marker_bytes = len(self.TRUNCATION_MARKER.encode("utf-8"))
        budget = max(0, self.MAX_TEXT_BYTES - marker_bytes)
        if self._turn_bytes + incoming_bytes <= budget:
            return False
        for p in self._text_parts:
            if self._turn_bytes + incoming_bytes <= budget:
                break
            text = p["chunks"][-1] if p.get("finalized") else "".join(p["chunks"])
            enc = text.encode("utf-8")
            drop = min(len(enc), max(0, (self._turn_bytes + incoming_bytes) - budget))
            keep = enc[drop:]
            # The cut is at the FRONT: advance the start to the next
            # character boundary (dropping from the end would strip the
            # whole remainder when the cut splits a character).
            i = 0
            kept = ""
            while i < len(keep):
                try:
                    kept = keep[i:].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    i += 1
            kept_b = len(kept.encode("utf-8"))
            self._turn_bytes -= p["bytes"] - kept_b
            p["chunks"] = [kept]
            p["bytes"] = kept_b
            p["finalized"] = True
        self._turn_truncated = True
        return True

    def _admit_text(self, text: str) -> str:
        """Admit incoming text under the ONE total turn bound.

        Oldest parts are trimmed first; if the incoming text ALONE exceeds
        the budget, its NEWEST bytes are kept. The result (possibly empty)
        is what the caller appends, so the newest useful output survives.
        """
        marker_bytes = len(self.TRUNCATION_MARKER.encode("utf-8"))
        budget = max(0, self.MAX_TEXT_BYTES - marker_bytes)
        enc = text.encode("utf-8")
        if self._turn_bytes + len(enc) <= budget:
            return text
        self._enforce_total_bound(len(enc))
        if self._turn_bytes + len(enc) <= budget:
            return text
        # Incoming alone exceeds the budget: keep the newest tail.
        keep = enc[len(enc) - budget:] if budget else b""
        while keep:
            try:
                return keep.decode("utf-8")
            except UnicodeDecodeError:
                keep = keep[1:]
        return ""

    def _append_to_part(self, part_id: str, text: str) -> None:
        """Append admitted text to an exact text part under the total bound."""
        if not text:
            return
        part = self._part_by_id(part_id)
        if part is None:
            part = self._part_by_id(part_id, create=True)
        admitted = self._admit_text(text)
        if not admitted:
            self._turn_truncated = True
            return
        part["chunks"].append(admitted)
        added = len(admitted.encode("utf-8"))
        part["bytes"] += added
        self._turn_bytes += added

    def _part_by_id(self, part_id: str, create: bool = False) -> Optional[Dict[str, Any]]:
        for p in self._text_parts:
            if p["id"] == part_id:
                return p
        if create:
            p = {"id": part_id, "chunks": [""], "bytes": 0, "finalized": False}
            self._text_parts.append(p)
            return p
        return None

    def _process_event_text(self, event_type: str, data: Dict[str, Any]) -> str:
        """Process event for multi-part text assembly and activity line.

        Native identity fidelity: real recorded events carry
        assistantMessageID (assistant message) and textID (text part).
        messageID/partID are accepted ONLY as legacy-vocabulary aliases and
        are never used when the native names are present.
        """
        activity = ""

        msg_id = (data.get("assistantMessageID")
                  or data.get("messageID") or data.get("messageId"))
        part_id = data.get("textID") or data.get("partID") or data.get("partId")

        if "text.started" in event_type:
            if msg_id and self._current_message_id and msg_id != self._current_message_id:
                # New assistant generation: clear the prior turn display.
                self._reset_turn()
            if msg_id:
                self._current_message_id = msg_id
            pid = part_id or f"part-{len(self._text_parts)}"
            self._part_by_id(pid, create=True)
            self._active_text_part_id = pid

        elif "text.delta" in event_type:
            if (msg_id and self._current_message_id
                    and msg_id != self._current_message_id):
                return activity  # stale delta for a previous generation
            pid = part_id or self._active_text_part_id
            if pid is None:
                return activity
            delta = data.get("delta", "")
            if delta:
                self._append_to_part(pid, delta)
                self._active_text_part_id = pid

        elif "text.ended" in event_type:
            pid = part_id or self._active_text_part_id
            if pid is not None:
                part = self._part_by_id(pid)
                if part is None:
                    part = self._part_by_id(pid, create=True)
                final = data.get("text")
                if final is not None:
                    # Durable final text REPLACES the live partial: subtract
                    # this part's previous contribution, then apply the
                    # authoritative text under the ONE total bound.
                    self._turn_bytes -= part["bytes"]
                    part["bytes"] = 0
                    part["chunks"] = [""]
                    admitted = self._admit_text(final)
                    if admitted:
                        part["chunks"] = [admitted]
                        part["bytes"] = len(admitted.encode("utf-8"))
                        self._turn_bytes += part["bytes"]
                    if admitted == final:
                        # Full authoritative text admitted: the display has
                        # converged, the earlier truncation marker no longer
                        # describes what the user sees.
                        self._turn_truncated = False
                part["finalized"] = True

        elif "tool.called" in event_type:
            # Tool calls between text parts must NOT erase earlier text.
            tool = data.get("tool") or data.get("name") or ""
            input_data = data.get("input", {})
            if isinstance(input_data, dict):
                path = input_data.get("path") or input_data.get("file") or ""
                if path:
                    activity = f"Read {os.path.basename(path)}"
                elif tool:
                    activity = f"Tool: {tool}"
                else:
                    activity = "Running tool..."
            else:
                activity = f"Tool: {tool}" if tool else "Running tool..."

        elif "reasoning.started" in event_type:
            activity = "Thinking..."

        elif "reasoning.ended" in event_type:
            activity = ""

        elif "step.started" in event_type:
            activity = "Step started"

        elif "step.ended" in event_type:
            finish = data.get("finish", "")
            if finish == "stop":
                activity = "Done"
            elif finish == "tool-calls":
                activity = "Tool calls..."
            else:
                activity = f"Step ended ({finish})" if finish else "Step ended"

        elif "step.failed" in event_type:
            err = data.get("error", {})
            msg = err.get("message", "Error") if isinstance(err, dict) else "Error"
            activity = f"FAILED: {msg[:60]}"

        elif "permission.asked" in event_type:
            activity = "Waiting for permission..."

        elif "question.asked" in event_type:
            activity = "Question asked"

        elif "prompted" in event_type:
            activity = "Prompted"

        return activity
