"""
viewer_live_stream.py - Live native SSE event worker for Queue Viewer Cockpit V1.

Authority contract:
- SQLite (viewer_observer) is the DURABLE/RECONCILIATION authority: session
  list, queue, hydration, final text.ended / step.ended convergence.
- THIS module is the token-stream authority for realtime projection. SQLite
  durable rows do NOT carry streaming text deltas (verified from recorded real
  OpenCode native history: session.next.text.delta count 0 in captured
  evidence, text.started/ended carry assistantMessageID/textID).

Transport:
- Exactly ONE live event connection (GET <endpoint>/api/event,
  text/event-stream) owned by a dedicated QThread via QtNetwork
  (QNetworkAccessManager/QNetworkReply) so shutdown can abort cleanly from
  the worker's own thread. Never an uninterruptible blocking urllib read.
- Never a second connection while one is alive.
- Reconnect backoff 1s/2s/5s/10s capped + jitter, with jitter for thundering
  -herd avoidance.

Native envelope (verified against the repository's recorded REAL OpenCode
native history and the 2.5.2 projector contract, which destructures
`{ type, properties }` from the raw SSE wrapper):
    { "id": ..., "type": "session.next.*", "properties": {...} }
with properties.sessionID / properties.assistantMessageID / properties.textID /
properties.delta / properties.text / properties.timestamp. The durable SQLite
rows carry the SAME payload under a `data` key, so `data` is accepted only as
the durable-row compatibility key; the LIVE path is `properties` first.
SSE data frames are parsed incrementally; data lines are accumulated per
frame until an empty line, then JSON-decoded.

Identity fidelity (P0): canonical native field names are
    assistantMessageID  - assistant message identity
    textID              - text part identity
    sessionID           - session identity
    timestamp           - native milliseconds (or ISO-8601 string)
No invented aliases are accepted for the primary parse path.

Session filter: only events whose sessionID equals the exact selected session
are forwarded, except transport-level events (server.connected,
server.heartbeat, server.instance.disposed).

UI coalescing: deltas are accumulated immediately in the worker; a bounded
immutable display snapshot is emitted at a 30-75 ms cadence, never one UI
update per token.
"""
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import (
    QObject, QThread, QTimer, pyqtSignal, pyqtSlot, QUrl, QByteArray,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

# Transport-level events that bypass the session filter.
TRANSPORT_EVENTS = frozenset({
    "server.connected",
    "server.heartbeat",
    "server.instance.disposed",
})

STREAM_LIVE = "LIVE"
STREAM_RECONNECTING = "RECONNECTING"
STREAM_DB_ONLY = "DB-ONLY"
STREAM_OFFLINE = "OFFLINE"

# UI render cadence bounds (ms)
UI_CADENCE_MIN_MS = 30
UI_CADENCE_MAX_MS = 75
DEFAULT_UI_CADENCE_MS = 50

# Reconnect backoff sequence (seconds) plus jitter fraction.
RECONNECT_BACKOFF_S = [1.0, 2.0, 5.0, 10.0]
RECONNECT_BACKOFF_CAP_S = 10.0
JITTER_FRACTION = 0.2


def parse_sse_frame(data_lines: List[bytes]) -> Optional[Dict[str, Any]]:
    """Parse one accumulated SSE frame's data lines into a JSON object.

    Returns None for keep-alive comments/empty frames or undecodable frames
    (never raises: a malformed server frame must not kill the stream).
    """
    if not data_lines:
        return None
    raw = b"\n".join(data_lines)
    if not raw.strip():
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
        return obj if isinstance(obj, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def extract_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Event properties dict from the native envelope.

    The LIVE SSE envelope carries `properties`; durable SQLite rows carry
    the same payload under `data`. `properties` wins; `data` is the
    compatibility key for durable-shaped payloads only.
    """
    props = payload.get("properties")
    if isinstance(props, dict):
        return props
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return {}


def extract_session_id(payload: Dict[str, Any]) -> Optional[str]:
    """Session identity from the native envelope: properties.sessionID."""
    sid = extract_payload(payload).get("sessionID")
    return sid if isinstance(sid, str) else None


def extract_event_type(payload: Dict[str, Any]) -> str:
    t = payload.get("type")
    return t if isinstance(t, str) else ""


def extract_timestamp(data: Dict[str, Any]) -> Optional[float]:
    """Native event timestamp in SECONDS; None when absent.

    Native carries milliseconds in properties.timestamp (durable rows also
    carry numeric ms); the projector additionally tolerates ISO-8601
    strings. Normalize to seconds.
    """
    ts = data.get("timestamp")
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)) and ts > 0:
        return ts / 1000.0 if ts > 1e11 else float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


@dataclass
class LiveSnapshot:
    """Bounded immutable display snapshot emitted at the UI cadence."""
    session_id: str
    text: str
    total_bytes: int
    truncated: bool
    seq: int  # monotonically increasing snapshot counter


class SseFrameParser:
    """Incremental SSE byte-stream parser. Feed raw bytes; complete frames
    come out as decoded JSON objects. Never raises."""

    def __init__(self):
        self._buffer = bytearray()
        self._data_lines: List[bytes] = []

    def feed(self, chunk: bytes) -> List[Dict[str, Any]]:
        self._buffer.extend(chunk)
        frames: List[Dict[str, Any]] = []
        while True:
            idx = self._buffer.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buffer[:idx]).rstrip(b"\r")
            del self._buffer[: idx + 1]
            if line == b"":
                frame = parse_sse_frame(self._data_lines)
                self._data_lines = []
                if frame is not None:
                    frames.append(frame)
            elif line.startswith(b":"):
                # SSE comment / keep-alive
                continue
            elif line.startswith(b"data:"):
                self._data_lines.append(line[5:].lstrip(b" "))
            # event:/id:/retry: lines are ignored: type/session identity are
            # carried inside the JSON envelope itself.
        return frames

    def reset(self):
        self._buffer = bytearray()
        self._data_lines = []


class LiveStreamWorker(QObject):
    """Single-connection SSE consumer. Lives in its own QThread.

    GUI control flows ONLY through queued signals (select_session,
    stop_worker, retarget_endpoint) connected after moveToThread.
    """

    # Bounded immutable display snapshot at UI cadence (worker -> GUI)
    snapshot_ready = pyqtSignal(object)          # LiveSnapshot
    # Live native event forwarded for state/turn processing (filtered)
    live_event = pyqtSignal(str, str, object)    # (session_id, type, data)
    # Native event timestamp for Last-event age
    live_event_time = pyqtSignal(str, float)     # (session_id, epoch_seconds)
    stream_status = pyqtSignal(str, str)         # (endpoint_url, status)
    stopped = pyqtSignal()

    # Queued command signals (GUI -> worker), connected AFTER moveToThread.
    select_session = pyqtSignal(str)
    retarget_endpoint = pyqtSignal(str)
    stop_worker = pyqtSignal()
    refresh_now = pyqtSignal()

    UI_CADENCE_MS = DEFAULT_UI_CADENCE_MS
    MAX_TOTAL_BYTES = 128 * 1024  # total current-turn UTF-8 bound
    TRUNCATION_MARKER = "\n\n[... output truncated at 128 KiB ...]"

    def __init__(self, endpoint_url: str = ""):
        super().__init__()
        self._endpoint_url = endpoint_url
        self._session_id: Optional[str] = None
        self._nam: Optional[QNetworkAccessManager] = None
        self._reply: Optional[QNetworkReply] = None
        self._parser = SseFrameParser()
        self._running = False
        self._connection_alive = False
        self._ever_connected = False
        self._aborting = False
        self._backoff_index = 0
        self._reconnect_timer: Optional[QTimer] = None

        # Current-turn model (live authority)
        self._current_message_id: Optional[str] = None
        self._parts: List[Dict[str, Any]] = []   # {textID, chunks, bytes, finalized, ended}
        self._total_bytes = 0
        self._truncated = False
        self._snapshot_seq = 0
        self._dirty = False

        self._ui_timer: Optional[QTimer] = None
        self._last_activity_ts = 0.0

    # ── Queued commands (worker thread) ─────────────────────────────

    @pyqtSlot()
    def start(self):
        if self._running:
            return
        self._running = True
        self._nam = QNetworkAccessManager(self)
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._emit_snapshot)
        self._ui_timer.start(self.UI_CADENCE_MS)
        if self._endpoint_url:
            self._open_stream()

    @pyqtSlot(str)
    def _on_select_session(self, session_id: str):
        target = session_id or None
        if target != self._session_id:
            self._session_id = target
            self._reset_turn()

    @pyqtSlot(str)
    def _on_retarget_endpoint(self, endpoint_url: str):
        url = endpoint_url or ""
        if url == self._endpoint_url:
            return
        self._endpoint_url = url
        self._abort_reply()
        self._connection_alive = False
        self._backoff_index = 0
        self._reset_turn()
        if self._running and url:
            self._open_stream()
        elif self._running:
            self.stream_status.emit("", STREAM_OFFLINE)

    @pyqtSlot()
    def _on_refresh(self):
        """F5: force an immediate reconnect attempt if not healthy."""
        if self._running and not self._connection_alive and self._endpoint_url:
            self._stop_reconnect_timer()
            self._open_stream()

    @pyqtSlot()
    def _on_stop(self):
        """Queued stop from the GUI. Aborts the reply from THIS thread."""
        self._running = False
        self._abort_reply()
        self._stop_reconnect_timer()
        if self._ui_timer:
            self._ui_timer.stop()
            self._ui_timer = None
        if self._nam:
            self._nam.deleteLater()
            self._nam = None
        self.stopped.emit()

    # Back-compat direct-call wrappers (worker-thread/tests only)
    def select(self, session_id: Optional[str]):
        self._on_select_session(session_id or "")
    def retarget(self, endpoint_url: str):
        self._on_retarget_endpoint(endpoint_url or "")
    def stop(self):
        self._on_stop()

    # ── Connection management (worker thread) ───────────────────────

    def _open_stream(self):
        if not self._running or not self._nam or not self._endpoint_url:
            return
        if self._reply is not None:
            return  # never a second SSE while one is alive/connecting
        url = self._endpoint_url.rstrip("/") + "/api/event"
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"Accept", b"text/event-stream")
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self._parser.reset()
        reply = self._nam.get(request)
        self._reply = reply
        self._connection_alive = False
        reply.readyRead.connect(self._on_ready_read)
        reply.errorOccurred.connect(self._on_error)
        reply.finished.connect(self._on_finished)
        # Emit an initial read in case data is already buffered.
        self._on_ready_read()

    def _abort_reply(self):
        self._aborting = True
        if self._reply is not None:
            reply = self._reply
            self._reply = None
            try:
                reply.readyRead.disconnect(self._on_ready_read)
            except TypeError:
                pass
            try:
                reply.errorOccurred.disconnect(self._on_error)
            except TypeError:
                pass
            try:
                reply.finished.disconnect(self._on_finished)
            except TypeError:
                pass
            reply.abort()
            reply.deleteLater()
        self._connection_alive = False
        self._aborting = False

    def _on_ready_read(self):
        if not self._running:
            return
        reply = self._reply
        if reply is None:
            return
        if not self._connection_alive:
            self._connection_alive = True
            self._backoff_index = 0
            self.stream_status.emit(self._endpoint_url, STREAM_LIVE)
        # Continuous drain: read everything available so the server-side
        # event queue cannot accumulate.
        while reply.bytesAvailable() > 0:
            chunk = bytes(reply.read(65536))
            if not chunk:
                break
            for frame in self._parser.feed(chunk):
                self._handle_frame(frame)

    def _on_error(self, code):
        if not self._running or self._aborting:
            return
        self._connection_alive = False
        self.stream_status.emit(self._endpoint_url, STREAM_RECONNECTING)
        self._schedule_reconnect()

    def _on_finished(self):
        """Server closed the stream (or transport failure surfaced only as
        finished): treat as disconnect unless we aborted it ourselves."""
        if not self._running or self._aborting:
            return
        reply = self._reply
        if reply is not None:
            self._reply = None
            try:
                reply.readyRead.disconnect(self._on_ready_read)
            except TypeError:
                pass
            try:
                reply.errorOccurred.disconnect(self._on_error)
            except TypeError:
                pass
            try:
                reply.finished.disconnect(self._on_finished)
            except TypeError:
                pass
            reply.deleteLater()
        if self._connection_alive:
            self._connection_alive = False
        self.stream_status.emit(self._endpoint_url, STREAM_RECONNECTING)
        self._schedule_reconnect()

    def _schedule_reconnect(self):
        if not self._running or self._reconnect_timer:
            return
        base = RECONNECT_BACKOFF_S[min(self._backoff_index, len(RECONNECT_BACKOFF_S) - 1)]
        self._backoff_index = min(self._backoff_index + 1, len(RECONNECT_BACKOFF_S) - 1)
        delay = base * (1.0 + random.uniform(-JITTER_FRACTION, JITTER_FRACTION))
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._reconnect_tick)
        self._reconnect_timer.start(int(max(0.1, delay) * 1000))

    def _stop_reconnect_timer(self):
        if self._reconnect_timer:
            self._reconnect_timer.stop()
            self._reconnect_timer = None

    def _reconnect_tick(self):
        self._reconnect_timer = None
        if not self._running or self._connection_alive:
            return
        self._open_stream()

    # ── Frame handling (worker thread) ──────────────────────────────

    def _handle_frame(self, payload: Dict[str, Any]):
        etype = extract_event_type(payload)
        if not etype:
            return
        data = extract_payload(payload)

        if etype in TRANSPORT_EVENTS:
            return  # connection health is tracked by reply state itself

        sid = extract_session_id(payload)
        if sid is None or sid != self._session_id:
            return  # NEVER display another session's text

        ts = extract_timestamp(data)
        if ts is not None:
            self._last_activity_ts = ts
            self.live_event_time.emit(sid, ts)
        else:
            now = time.time()
            self._last_activity_ts = now
            self.live_event_time.emit(sid, now)

        if etype == "session.next.step.started":
            mid = data.get("assistantMessageID")
            if isinstance(mid, str) and mid != self._current_message_id:
                # New assistant generation: reset the visible current turn.
                self._reset_turn()
                self._current_message_id = mid
        elif etype == "session.next.text.started":
            mid = data.get("assistantMessageID")
            tid = data.get("textID")
            if isinstance(mid, str) and mid != self._current_message_id:
                self._reset_turn()
                self._current_message_id = mid
            if isinstance(tid, str):
                self._part_by_id(tid, create=True)
        elif etype == "session.next.text.delta":
            mid = data.get("assistantMessageID")
            tid = data.get("textID")
            delta = data.get("delta", "")
            if not (isinstance(mid, str) and isinstance(tid, str) and isinstance(delta, str)):
                return
            if mid != self._current_message_id:
                return  # stale delta for a previous generation: ignored
            self._append_delta(tid, delta)
        elif etype == "session.next.text.ended":
            mid = data.get("assistantMessageID")
            tid = data.get("textID")
            final = data.get("text")
            if isinstance(mid, str) and mid != self._current_message_id:
                return  # stale ended for a previous generation
            if isinstance(tid, str) and isinstance(final, str):
                self._finalize_part(tid, final)

        self.live_event.emit(sid, etype, data)
        self._dirty = True

    # ── Turn model (worker thread) ──────────────────────────────────

    def _reset_turn(self):
        self._current_message_id = None
        self._parts = []
        self._total_bytes = 0
        self._truncated = False
        self._dirty = True

    def _part_by_id(self, text_id: str, create: bool = False) -> Optional[Dict[str, Any]]:
        for p in self._parts:
            if p["textID"] == text_id:
                return p
        if create:
            p = {"textID": text_id, "chunks": [""], "bytes": 0,
                 "finalized": False, "ended": False}
            self._parts.append(p)
            return p
        return None

    def _append_delta(self, text_id: str, delta: str):
        part = self._part_by_id(text_id, create=True)
        if part["ended"]:
            return  # ended part is authoritative; live deltas cannot reopen it
        add_b = delta.encode("utf-8")
        budget = self.MAX_TOTAL_BYTES - len(self.TRUNCATION_MARKER.encode("utf-8"))
        if self._total_bytes + len(add_b) <= budget:
            part["chunks"].append(delta)
            part["bytes"] += len(add_b)
            self._total_bytes += len(add_b)
            return
        # Over budget: trim OLDEST parts first, preserving the newest useful
        # output; if the incoming delta alone exceeds the budget, keep its
        # newest tail. Never split UTF-8 characters.
        self._apply_total_truncation(len(add_b))
        if self._total_bytes + len(add_b) <= budget:
            part["chunks"].append(delta)
            part["bytes"] += len(add_b)
            self._total_bytes += len(add_b)
            return
        keep = add_b[len(add_b) - budget:] if budget else b""
        while keep:
            try:
                kept_text = keep.decode("utf-8")
                break
            except UnicodeDecodeError:
                keep = keep[1:]
        else:
            kept_text = ""
        if kept_text:
            kept_b = len(kept_text.encode("utf-8"))
            self._total_bytes -= part["bytes"]
            part["chunks"] = [kept_text]
            part["bytes"] = kept_b
            self._total_bytes += kept_b
        self._truncated = True

    def _finalize_part(self, text_id: str, final: str):
        part = self._part_by_id(text_id, create=True)
        final_b = final.encode("utf-8")
        budget = self.MAX_TOTAL_BYTES - len(self.TRUNCATION_MARKER.encode("utf-8"))
        # Replace this part's contribution to the total, respecting the bound.
        self._total_bytes -= part["bytes"]
        part["bytes"] = 0
        part["chunks"] = [""]
        if self._total_bytes + len(final_b) <= budget:
            part["chunks"] = [final]
            part["bytes"] = len(final_b)
            self._total_bytes += len(final_b)
            part["ended"] = True
            self._truncated = False  # converged on full authoritative text
        else:
            # Final text alone exceeds the budget: keep its newest tail.
            keep = final_b[len(final_b) - budget:] if budget else b""
            while keep:
                try:
                    kept_text = keep.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    keep = keep[1:]
            else:
                kept_text = ""
            part["chunks"] = [kept_text]
            part["bytes"] = len(kept_text.encode("utf-8"))
            self._total_bytes += part["bytes"]
            part["ended"] = True
            self._truncated = True

    def _apply_total_truncation(self, incoming_bytes: int = 0):
        """ONE total bound across the whole rendered turn. Trims OLDEST text
        first (preserving the newest useful output), never splitting UTF-8."""
        budget = self.MAX_TOTAL_BYTES - len(self.TRUNCATION_MARKER.encode("utf-8"))
        # Trim oldest parts until total + incoming fits.
        for p in self._parts:
            if self._total_bytes + incoming_bytes <= budget:
                break
            if p["finalized"] or p["chunks"]:
                text = "".join(p["chunks"]) if not p["finalized"] else p["chunks"][-1]
                enc = text.encode("utf-8")
                drop = min(p["bytes"], max(0, (self._total_bytes + incoming_bytes) - budget))
                keep = enc[drop:]
                # The cut is at the FRONT: advance to the next character
                # boundary instead of stripping from the end.
                i = 0
                kept_text = ""
                while i < len(keep):
                    try:
                        kept_text = keep[i:].decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        i += 1
                p["chunks"] = [kept_text]
                p["bytes"] = len(kept_text.encode("utf-8"))
                p["finalized"] = True
                self._total_bytes = sum(q["bytes"] for q in self._parts)
        self._truncated = True

    def _render(self) -> str:
        text = "".join("".join(p["chunks"]) for p in self._parts)
        if self._truncated:
            text = text + self.TRUNCATION_MARKER
        return text

    # ── Coalesced UI output (worker thread) ─────────────────────────

    @pyqtSlot()
    def _emit_snapshot(self):
        if not self._running or not self._dirty or not self._session_id:
            return
        self._snapshot_seq += 1
        snap = LiveSnapshot(
            session_id=self._session_id,
            text=self._render(),
            total_bytes=self._total_bytes,
            truncated=self._truncated,
            seq=self._snapshot_seq,
        )
        self._dirty = False
        self.snapshot_ready.emit(snap)

    # ── Introspection (tests) ───────────────────────────────────────

    @property
    def connection_alive(self) -> bool:
        return self._connection_alive

    @property
    def has_reply(self) -> bool:
        return self._reply is not None

    @property
    def current_message_id(self):
        return self._current_message_id

    def debug_render(self) -> str:
        return self._render()

    def debug_handle_frame(self, payload: Dict[str, Any]):
        self._handle_frame(payload)


def build_live_worker(endpoint_url: str = "") -> LiveStreamWorker:
    """Factory used by the Viewer and tests."""
    return LiveStreamWorker(endpoint_url)
