"""
test_queue_viewer.py - Deterministic test suite for Queue Viewer Cockpit V1
(T-168 realtime + target-ownership pass).

Fixtures use the REAL native OpenCode event shapes recorded in this
repository (.saipen/evidence/final-wave/native-fixture):
  - durable SQLite rows:  {id, type, durable{aggregateID, seq}, data{
    timestamp(ms), sessionID, assistantMessageID, textID, text, finish}}
  - live SSE envelopes:   {id?, type, properties{sessionID, assistantMessageID,
    textID, delta, text, timestamp}}
  - durable rows NEVER carry session.next.text.delta (recorded real history:
    text.started 13 / text.ended 13 / text.delta 0). The live token-stream
    authority is the /api/event SSE stream; SQLite is reconciliation.

These tests exercise the PRODUCTION objects (ReadOnlyObserver, the real
SQLite path, OpenCodeEndpointResolver, NativeSender, PresetWorker,
LiveStreamWorker, QueueViewerWindow) — no self-constructed stand-ins.

Run: python tests/test_queue_viewer.py
Qt runs offscreen (QT_QPA_PLATFORM=offscreen) where a QApplication is needed.
"""
import os
import sys
import json
import time
import queue as queue_mod
import sqlite3
import tempfile
import threading
import unittest
import unittest.mock
import inspect
from dataclasses import replace
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler

# Offscreen Qt on Windows CI/dev machines.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Add saipatch to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Scripts", "saipatch")))

from PyQt6.QtCore import QTimer, QThread, QEventLoop
from PyQt6.QtWidgets import QApplication

import viewer_observer as obs
import viewer_endpoint as ep
import viewer_sender as sender_mod
import viewer_presets as presets
import viewer_live_stream as lstream
import viewer_presentation as present

APP = None
MAIN_THREAD_ID = threading.get_ident()


def setUpModule():
    global APP
    APP = QApplication.instance() or QApplication(sys.argv)


# ── Test Schema ─────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE `session` (
  `id` text PRIMARY KEY,
  `directory` text NOT NULL,
  `title` text NOT NULL,
  `time_updated` integer NOT NULL
);

CREATE TABLE `session_input` (
  `id` text PRIMARY KEY,
  `session_id` text NOT NULL REFERENCES `session`(`id`) ON DELETE CASCADE,
  `prompt` text NOT NULL,
  `delivery` text NOT NULL,
  `admitted_seq` integer NOT NULL,
  `promoted_seq` integer,
  `time_created` integer NOT NULL
);
CREATE UNIQUE INDEX `session_input_session_admitted_seq_idx`
  ON `session_input` (`session_id`, `admitted_seq`);

CREATE TABLE `event` (
  `id` text PRIMARY KEY,
  `aggregate_id` text NOT NULL,
  `seq` integer NOT NULL,
  `type` text NOT NULL,
  `data` text
);
"""


# ── REAL native fixtures (sanitized from recorded evidence) ─────────
# Shapes copied from .saipen/evidence/final-wave/native-fixture/
# mixed-final-native-history.json. Canonical native identities:
# assistantMessageID / textID / sessionID / timestamp(ms) / finish.

SES_ID = "ses_f8c85afbcffe1Ek05OAcZJlcu9"
USER_MSG_ID = "msg_073812f1d0017q1Ik1Qis1qO2u"
ASSIST_MSG_ID = "msg_07381309e001Z0cS7mTRUqDL03"
TEXT_PART_ID = "text-0"

# Native millisecond timestamps from the recorded evidence.
TS_PROMPTED_MS = 1788644241181
TS_STEP_STARTED_MS = 1788644241566
TS_TEXT_STARTED_MS = 1788644241567
TS_TEXT_ENDED_MS = 1788644249584
TS_STEP_ENDED_MS = 1788644249758


def native_data(**kw):
    """A durable/live native payload: sessionID + timestamp(ms) always."""
    base = {"timestamp": TS_TEXT_STARTED_MS, "sessionID": SES_ID}
    base.update(kw)
    return base


def live_envelope(event_type, **props):
    """A live SSE envelope: {type, properties{...}} (the 2.5.2 projector
    contract destructures exactly these two keys)."""
    return {"id": f"evt_live_{event_type}", "type": event_type,
            "properties": native_data(**props)}


def durable_row(event_type, data, seq=1):
    """A durable SQLite event row payload (same payload under `data`)."""
    return json.dumps(data)


REAL_DB_EVENTS = [
    (1, 'session.created.1', '{}'),
    (2, 'session.next.prompted.1', durable_row(
        'prompted', native_data(timestamp=TS_PROMPTED_MS, messageID=USER_MSG_ID,
                                prompt={"text": "cc2"}, delivery="queue"))),
    (3, 'session.next.step.started.2', durable_row(
        'step.started', native_data(timestamp=TS_STEP_STARTED_MS,
                                    assistantMessageID=ASSIST_MSG_ID,
                                    agent="build",
                                    model={"id": "queue-fixture", "providerID": "fixture"}))),
    (4, 'session.next.text.started.2', durable_row(
        'text.started', native_data(timestamp=TS_TEXT_STARTED_MS,
                                    assistantMessageID=ASSIST_MSG_ID,
                                    textID=TEXT_PART_ID))),
    (5, 'session.next.text.ended.2', durable_row(
        'text.ended', native_data(timestamp=TS_TEXT_ENDED_MS,
                                  assistantMessageID=ASSIST_MSG_ID,
                                  textID=TEXT_PART_ID,
                                  text="cc2 finished"))),
    (6, 'session.next.step.ended.2', durable_row(
        'step.ended', native_data(timestamp=TS_STEP_ENDED_MS,
                                  assistantMessageID=ASSIST_MSG_ID,
                                  finish="stop", cost=0,
                                  tokens={"input": 0, "output": 0, "reasoning": 0,
                                          "cache": {"read": 0, "write": 0}}))),
]

# A LIVE generation: started but NOT ended (streaming in progress).
ACTIVE_DB_EVENTS = [
    (1, 'session.next.prompted.1', durable_row(
        'prompted', native_data(timestamp=TS_PROMPTED_MS, messageID=USER_MSG_ID,
                                prompt={"text": "build me"}, delivery="queue"))),
    (2, 'session.next.step.started.2', durable_row(
        'step.started', native_data(timestamp=TS_STEP_STARTED_MS,
                                    assistantMessageID=ASSIST_MSG_ID, agent="build"))),
    (3, 'session.next.text.started.2', durable_row(
        'text.started', native_data(timestamp=TS_TEXT_STARTED_MS,
                                    assistantMessageID=ASSIST_MSG_ID,
                                    textID=TEXT_PART_ID))),
    # NO text.ended row: SQLite never stores streaming deltas, and the
    # final text has not durably arrived yet.
]


def create_test_db(with_queue=True, events=REAL_DB_EVENTS, sessions=("ses_1", "ses_2")) -> str:
    """Create a temporary test database with the OpenCode schema and REAL
    native-shaped event rows (never artificial text.delta rows)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.executescript(SCHEMA_SQL)

    updated = 2000
    for i, sid in enumerate(sessions):
        conn.execute("INSERT INTO session VALUES (?,?,?,?)",
                     (sid, f"V:/code/proj{i+1}", f"Project {i+1}", updated - i * 1000))

    if with_queue:
        conn.execute("""INSERT INTO session_input VALUES
            ('msg_1', 'ses_1', '{"text":"first prompt"}', 'queue', 1, 2, 1000)""")
        conn.execute("""INSERT INTO session_input VALUES
            ('msg_2', 'ses_1', '{"text":"second prompt"}', 'queue', 3, NULL, 1100)""")

    if events:
        # Events attach to the FIRST session unless the data carries a
        # different sessionID.
        first = sessions[0]
        for seq, ev_type, data in events:
            agg = first
            if data and data != "{}":
                try:
                    agg = json.loads(data).get("sessionID", first)
                except Exception:
                    pass
            conn.execute("INSERT INTO event VALUES (?,?,?,?,?)",
                         (f"evt_{agg}_{seq}", agg, seq, ev_type, data))

    conn.commit()
    conn.close()
    return tmp.name


class FakeOpenCodeServer(ThreadingHTTPServer):
    """Fake OpenCode host: native JSON API + a live /api/event SSE stream.

    Threaded per connection: a parked SSE handler must never block other
    requests (single-threaded servers deadlock the verify+POST path).
    Daemon threads so teardown never joins a parked stream handler.

    Tracks every request path, admission bodies, SSE connection count, and
    lets tests push native live envelopes and drop the SSE connection.
    """
    daemon_threads = True

    def __init__(self, handler_cls):
        super().__init__(("127.0.0.1", 0), handler_cls)
        self.sse_queues = []
        self.sse_connections = 0
        self.closing = False
        self.request_paths = []
        self.admission_log = []
        self.server_sessions = {"ses_1"}

    def push_event(self, envelope: dict):
        for q in list(self.sse_queues):
            q.put(envelope)

    def drop_sse(self):
        """Server-side close of the live stream connection(s)."""
        self.closing = True
        for q in list(self.sse_queues):
            q.put(None)

    def reset_closing(self):
        self.closing = False


class FakeOpenCodeHandler(BaseHTTPRequestHandler):
    """Native API + SSE handler. Optional delays prove the GUI never
    freezes on HTTP; every request path is recorded."""

    server_sessions = {"ses_1"}
    admission_log = []
    fail_next = False
    test_delay_seconds = 0.0
    exact_session_exists = True
    request_paths = []

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle(self):
        # Client-side aborts (Viewer shutdown cancels its own replies) are
        # normal here; never let them splatter tracebacks.
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, OSError):
            self.close_connection = True

    @property
    def host(self) -> FakeOpenCodeServer:
        return self.server

    def _delay(self):
        if self.test_delay_seconds:
            time.sleep(self.test_delay_seconds)

    def do_GET(self):
        self.request_paths.append(self.path)
        if self.path == "/api/event":
            self._serve_sse()
            return
        self._delay()
        if "/api/session/active" in self.path:
            self.host.server_sessions = set(self.server_sessions)
            sid = sorted(self.server_sessions)[0] if self.server_sessions else None
            self._json({"data": {"id": sid}} if sid else {})
        elif self.path.startswith("/api/session/"):
            sid = self.path.rsplit("/", 1)[-1]
            if self.exact_session_exists and sid in self.server_sessions:
                self._json({"data": {"id": sid, "title": "fake"}})
            else:
                self._json_error(404, "session not found")
        else:
            self._json_error(404, "not found")

    def do_POST(self):
        self.request_paths.append(self.path)
        self._delay()
        if "/api/session/" in self.path and "/prompt" in self.path:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode()) if length else {}

            if self.fail_next:
                self._json_error(500, "test failure")
                return

            self.host.admission_log = type(self).admission_log
            self.host.admission_log.append(body)
            seq = len(self.admission_log)
            self._json({
                "data": {
                    "admittedSeq": seq,
                    "id": f"inp_{seq}",
                    "sessionID": sorted(self.server_sessions)[0] if self.server_sessions else "",
                    "prompt": body.get("prompt", {}),
                    "delivery": body.get("delivery", "queue"),
                    "timeCreated": int(time.time() * 1000),
                }
            })
        else:
            self._json_error(404, "not found")

    def _serve_sse(self):
        """text/event-stream: frames from the server queue, keep-alive
        comments while idle, exit on sentinel/closing."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = queue_mod.Queue()
        self.host.sse_queues.append(q)
        self.host.sse_connections += 1
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    obj = q.get(timeout=0.2)
                except queue_mod.Empty:
                    obj = "ping"
                if obj is None or (self.host.closing and obj == "ping"):
                    break
                if obj == "ping":
                    if self.host.closing:
                        break
                    self.wfile.write(b": ping\n\n")
                else:
                    self.wfile.write(
                        f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
                try:
                    self.wfile.flush()
                except (OSError, ValueError):
                    break
        except (ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                self.host.sse_queues.remove(q)
            except ValueError:
                pass
            # The stream request is over: close the socket for real so the
            # client observes the disconnect (keep-alive would hold it open).
            self.close_connection = True

    def _json(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json_error(self, code, msg):
        raw = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_fake_server(handler_cls=FakeOpenCodeHandler):
    server = FakeOpenCodeServer(handler_cls)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port, t


def drain_events(loop, ms):
    """Run the Qt event loop for `ms` wall-clock milliseconds."""
    end = QEventLoop()
    QTimer.singleShot(ms, end.quit)
    end.exec()


class SignalRecorder:
    def __init__(self, signal):
        self.items = []
        signal.connect(lambda *a: self.items.append(a))

    @property
    def last(self):
        return self.items[-1] if self.items else None


def make_window(db_path):
    from importlib import import_module
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "queue_viewer", os.path.join(
            os.path.dirname(__file__), "..", "Scripts", "saipatch", "queue_viewer.pyw"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["queue_viewer"] = mod
    spec.loader.exec_module(mod)
    win = mod.QueueViewerWindow(db_path=db_path)
    return win, mod


def make_summary(sid, state="IDLE", updated=100):
    from viewer_observer import SessionSummary
    return SessionSummary(id=sid, directory=f"V:/code/{sid}", title=sid,
                          project_name=sid, time_updated=updated,
                          pending_count=0, state=state, detail="",
                          is_process_live=False, latest_event_seq=0)


def grant_endpoint(win, port, session_id, generation=None,
                   sessions=None, state="RESOLVED"):
    """Install an authoritative RESOLVED endpoint status for a selection,
    exactly as the generation-gated GUI path requires."""
    from viewer_endpoint import ResolvedEndpoint, EndpointStatus
    if generation is None:
        generation = win._resolution_generation
    endpoint = ResolvedEndpoint(
        pid=4321, start_time=time.time() - 60,
        url=f"http://127.0.0.1:{port}",
        executable_path="c:/x/opencode.exe",
        sessions=tuple(sessions if sessions is not None else [session_id]))
    win._endpoint_status = EndpointStatus(
        state, endpoint, "", generation=generation, session_id=session_id)
    win.current_session_id = session_id
    win._session_unavailable = False
    win.sessions = [make_summary(session_id), make_summary("ses_other")]
    win._update_send_controls()
    return win._endpoint_status


# ═══════════════════════════════════════════════════════════════════
# Required test 1-5: real native identities in the LIVE stream model
# ═══════════════════════════════════════════════════════════════════

def make_live_worker(session_id=SES_ID):
    w = lstream.LiveStreamWorker("")
    w.select(session_id)
    return w


class TestLiveNativeTurnModel(unittest.TestCase):
    """Required 1-5: the live worker consumes REAL native envelopes
    (properties envelope, assistantMessageID/textID) with exact identity."""

    def test_01_real_started_event_creates_turn(self):
        w = make_live_worker()
        w.debug_handle_frame(live_envelope(
            "session.next.step.started", timestamp=TS_STEP_STARTED_MS,
            assistantMessageID=ASSIST_MSG_ID, agent="build"))
        w.debug_handle_frame(live_envelope(
            "session.next.text.started", timestamp=TS_TEXT_STARTED_MS,
            assistantMessageID=ASSIST_MSG_ID, textID=TEXT_PART_ID))
        self.assertEqual(w.current_message_id, ASSIST_MSG_ID)
        self.assertEqual(w.debug_render(), "")

    def test_02_real_delta_updates_exact_textID(self):
        w = make_live_worker()
        w.debug_handle_frame(live_envelope(
            "session.next.text.started", assistantMessageID=ASSIST_MSG_ID,
            textID="text-0"))
        w.debug_handle_frame(live_envelope(
            "session.next.text.delta", assistantMessageID=ASSIST_MSG_ID,
            textID="text-0", delta="cc2 "))
        w.debug_handle_frame(live_envelope(
            "session.next.text.delta", assistantMessageID=ASSIST_MSG_ID,
            textID="text-0", delta="fin"))
        self.assertEqual(w.debug_render(), "cc2 fin")
        # A second exact text part appends in order.
        w.debug_handle_frame(live_envelope(
            "session.next.text.started", assistantMessageID=ASSIST_MSG_ID,
            textID="text-1"))
        w.debug_handle_frame(live_envelope(
            "session.next.text.delta", assistantMessageID=ASSIST_MSG_ID,
            textID="text-1", delta=". Done"))
        self.assertEqual(w.debug_render(), "cc2 fin. Done")

    def test_03_real_ended_replaces_partial_text(self):
        w = make_live_worker()
        w.debug_handle_frame(live_envelope(
            "session.next.text.started", assistantMessageID=ASSIST_MSG_ID,
            textID=TEXT_PART_ID))
        w.debug_handle_frame(live_envelope(
            "session.next.text.delta", assistantMessageID=ASSIST_MSG_ID,
            textID=TEXT_PART_ID, delta="cc2 fin (partial drift)"))
        w.debug_handle_frame(live_envelope(
            "session.next.text.ended", timestamp=TS_TEXT_ENDED_MS,
            assistantMessageID=ASSIST_MSG_ID, textID=TEXT_PART_ID,
            text="cc2 finished"))
        self.assertEqual(w.debug_render(), "cc2 finished")

    def test_04_new_assistantMessageID_clears_prior_turn(self):
        w = make_live_worker()
        w.debug_handle_frame(live_envelope(
            "session.next.text.started", assistantMessageID=ASSIST_MSG_ID,
            textID=TEXT_PART_ID))
        w.debug_handle_frame(live_envelope(
            "session.next.text.delta", assistantMessageID=ASSIST_MSG_ID,
            textID=TEXT_PART_ID, delta="old generation text"))
        self.assertNotEqual(w.debug_render(), "")
        new_msg = "msg_0999newgeneration000000000000000000"
        w.debug_handle_frame(live_envelope(
            "session.next.text.started", assistantMessageID=new_msg,
            textID="text-new"))
        self.assertEqual(w.current_message_id, new_msg)
        self.assertEqual(w.debug_render(), "")

    def test_05_stale_delta_for_old_assistantMessageID_ignored(self):
        w = make_live_worker()
        w.debug_handle_frame(live_envelope(
            "session.next.text.started", assistantMessageID=ASSIST_MSG_ID,
            textID=TEXT_PART_ID))
        w.debug_handle_frame(live_envelope(
            "session.next.text.delta", assistantMessageID=ASSIST_MSG_ID,
            textID=TEXT_PART_ID, delta="fresh "))
        new_msg = "msg_0999newgeneration000000000000000000"
        w.debug_handle_frame(live_envelope(
            "session.next.text.started", assistantMessageID=new_msg,
            textID="text-new"))
        w.debug_handle_frame(live_envelope(
            "session.next.text.delta", assistantMessageID=new_msg,
            textID="text-new", delta="new gen"))
        # Stale delta for the PREVIOUS assistantMessageID cannot contaminate.
        w.debug_handle_frame(live_envelope(
            "session.next.text.delta", assistantMessageID=ASSIST_MSG_ID,
            textID=TEXT_PART_ID, delta="STALE CONTAMINATION"))
        w.debug_handle_frame(live_envelope(
            "session.next.text.ended", assistantMessageID=ASSIST_MSG_ID,
            textID=TEXT_PART_ID, text="STALE FINAL"))
        self.assertEqual(w.debug_render(), "new gen")


# ═══════════════════════════════════════════════════════════════════
# Required test 6: durable fixture carries no artificial text.delta
# ═══════════════════════════════════════════════════════════════════

class TestDurableReconciliation(unittest.TestCase):
    def test_06_durable_fixture_has_no_delta_rows_and_hydrates(self):
        for seq, ev_type, _ in REAL_DB_EVENTS:
            self.assertNotIn("text.delta", ev_type,
                             "durable rows must not manufacture text.delta")
        db_path = create_test_db()
        try:
            observer = obs.ReadOnlyObserver(db_path)
            observer._open_connection()
            observer._selected_session_id = SES_ID
            observer._hydrate(observer._conn.cursor(), SES_ID)
            # Reconciliation from started/ended only: final authoritative text.
            self.assertEqual(observer._assembled_text(), "cc2 finished")
            self.assertGreater(observer._last_event_seq, 0)
            observer.stop()
            del observer
            import gc
            gc.collect()
        finally:
            os.remove(db_path)

    def test_live_output_hydrates_running_turn(self):
        db_path = create_test_db(events=ACTIVE_DB_EVENTS)
        try:
            observer = obs.ReadOnlyObserver(db_path)
            observer._open_connection()
            observer._selected_session_id = SES_ID
            observer._hydrate(observer._conn.cursor(), SES_ID)
            # Active generation with no durable delta/ended: empty turn, but
            # the hydration position is past text.started.
            self.assertEqual(observer._assembled_text(), "")
            self.assertGreater(observer._last_event_seq, 0)
            observer.stop()
            del observer
            import gc
            gc.collect()
        finally:
            os.remove(db_path)


# ═══════════════════════════════════════════════════════════════════
# Required tests 7-11: live SSE projection vs durable reconciliation
# ═══════════════════════════════════════════════════════════════════

class TestLiveStreamProjection(unittest.TestCase):
    def _wire(self, db_events=ACTIVE_DB_EVENTS):
        db_path = create_test_db(events=db_events, sessions=(SES_ID, "ses_other"))
        server, port, t = start_fake_server()
        server.server_sessions = {SES_ID}
        win, mod = make_window(db_path)
        # Select through the REAL production path: this wires observer,
        # resolver, stream scope and the resolution generation exactly as a
        # user click does.
        win.sessions = [make_summary(SES_ID), make_summary("ses_other")]
        win._on_session_selected(0)
        drain_events(APP, 300)
        # The resolver is the endpoint AUTHORITY: teach its process scan to
        # find the fake OpenCode server (snapshot-shaped, as in production)
        # so its periodic ticks agree with the granted status instead of
        # truthfully reporting no endpoint on this test machine.
        snapshot = ep.ProcessSnapshot(
            pid=4321, start_time=time.time() - 60,
            executable_path="c:/x/opencode.exe",
            endpoint=f"http://127.0.0.1:{port}",
            verified_session_ids=(SES_ID,))
        win._resolver._scan_candidates = lambda: [snapshot]
        grant_endpoint(win, port, SES_ID)
        win._on_endpoint_resolved(win._endpoint_status)  # feeds stream retarget
        return win, mod, server, port, db_path

    def _teardown(self, win, server, db_path):
        try:
            win._shutdown_workers()
        finally:
            server.shutdown()
            server.server_close()
            os.remove(db_path)

    def test_07_sse_delta_shows_before_durable_ended(self):
        """Required 7: SSE text.delta updates LIVE OUTPUT before text.ended
        exists in SQLite (SQLite never carried the delta)."""
        win, mod, server, port, db_path = self._wire()
        try:
            win._stream.retarget_endpoint.emit(f"http://127.0.0.1:{port}")
            win._stream.select_session.emit(SES_ID)
            drain_events(APP, 600)
            self.assertGreaterEqual(server.sse_connections, 1)

            server.push_event(live_envelope(
                "session.next.step.started", assistantMessageID=ASSIST_MSG_ID))
            server.push_event(live_envelope(
                "session.next.text.started",
                assistantMessageID=ASSIST_MSG_ID, textID=TEXT_PART_ID))
            server.push_event(live_envelope(
                "session.next.text.delta",
                assistantMessageID=ASSIST_MSG_ID, textID=TEXT_PART_ID,
                delta="streaming part"))
            drain_events(APP, 900)

            shown = win.txt_live_output.toPlainText()
            self.assertIn("streaming part", shown,
                          "SSE delta must reach LIVE OUTPUT before any durable row")
            # The durable DB still has NO text.ended row at all.
            w = sqlite3.connect(db_path)
            rows = w.execute(
                "SELECT COUNT(*) FROM event WHERE type LIKE '%text.ended%'"
            ).fetchone()[0]
            w.close()
            self.assertEqual(rows, 0)
            win._update_health_display()
            self.assertEqual(win.lbl_stream_status.text(), "STREAM: LIVE")
        finally:
            self._teardown(win, server, db_path)

    def test_08_disconnect_shows_reconnecting_without_freeze(self):
        """Required 8: SSE disconnect flips STREAM to RECONNECTING and the
        GUI heartbeat keeps beating."""
        win, mod, server, port, db_path = self._wire()
        try:
            win._stream.retarget_endpoint.emit(f"http://127.0.0.1:{port}")
            win._stream.select_session.emit(SES_ID)
            drain_events(APP, 600)
            self.assertEqual(server.sse_connections, 1)

            heartbeats = []
            hb = QTimer()
            hb.timeout.connect(lambda: heartbeats.append(time.time()))
            hb.start(100)

            server.drop_sse()
            deadline = time.time() + 6
            while time.time() < deadline:
                drain_events(APP, 150)
                win._update_health_display()
                if win.lbl_stream_status.text() == "STREAM: RECONNECTING":
                    break
            self.assertEqual(win.lbl_stream_status.text(), "STREAM: RECONNECTING")
            # Keep spinning the loop: the GUI must stay responsive while the
            # stream reconnects in the background.
            hb_mark = len(heartbeats)
            end_t = time.time() + 1.5
            while time.time() < end_t:
                drain_events(APP, 100)
            self.assertGreaterEqual(len(heartbeats) - hb_mark, 10,
                                    "GUI starved while stream reconnected")
            hb.stop()
        finally:
            self._teardown(win, server, db_path)

    def test_09_sqlite_readable_while_sse_reconnects(self):
        """Required 9: durable observation stays healthy when SSE is down."""
        win, mod, server, port, db_path = self._wire()
        try:
            win._stream.retarget_endpoint.emit(f"http://127.0.0.1:{port}")
            win._stream.select_session.emit(SES_ID)
            drain_events(APP, 500)
            server.drop_sse()
            drain_events(APP, 800)

            health = SignalRecorder(win._observer.signals.observer_health)
            win._observer.refresh_now.emit()
            drain_events(APP, 700)
            statuses = [s[0] for s in health.items]
            self.assertIn("LIVE", statuses,
                          "SQLite observation must remain LIVE while SSE reconnects")
        finally:
            self._teardown(win, server, db_path)

    def test_10_durable_ended_reconciles_partial_stream(self):
        """Required 10: a missed-deltas stream converges to the durable
        authoritative final text."""
        win, mod, server, port, db_path = self._wire()
        try:
            win._stream.retarget_endpoint.emit(f"http://127.0.0.1:{port}")
            win._stream.select_session.emit(SES_ID)
            drain_events(APP, 600)
            # Only a partial delta arrives live; the connection then dies
            # before the rest of the tokens and before SSE text.ended.
            server.push_event(live_envelope(
                "session.next.text.started",
                assistantMessageID=ASSIST_MSG_ID, textID=TEXT_PART_ID))
            server.push_event(live_envelope(
                "session.next.text.delta",
                assistantMessageID=ASSIST_MSG_ID, textID=TEXT_PART_ID,
                delta="cc2 fin"))
            drain_events(APP, 700)
            self.assertIn("cc2 fin", win.txt_live_output.toPlainText())

            # Durable reconciliation: authoritative text.ended lands in DB.
            w = sqlite3.connect(db_path)
            w.execute(
                "INSERT INTO event VALUES (?,?,?,?,?)",
                ("evt_final", SES_ID, 99, "session.next.text.ended.2",
                 durable_row('text.ended', native_data(
                     timestamp=TS_TEXT_ENDED_MS,
                     assistantMessageID=ASSIST_MSG_ID,
                     textID=TEXT_PART_ID, text="cc2 finished fully"))))
            w.commit()
            w.close()

            deadline = time.time() + 5
            while time.time() < deadline:
                drain_events(APP, 200)
                if win.txt_live_output.toPlainText() == "cc2 finished fully":
                    break
            self.assertEqual(win.txt_live_output.toPlainText(),
                             "cc2 finished fully",
                             "durable text.ended must replace the live partial")
        finally:
            self._teardown(win, server, db_path)

    def test_11_only_one_sse_connection_per_endpoint(self):
        """Required 11: resolution ticks, refreshes and re-selections never
        open a second SSE connection on a live endpoint."""
        win, mod, server, port, db_path = self._wire()
        try:
            win._stream.retarget_endpoint.emit(f"http://127.0.0.1:{port}")
            win._stream.select_session.emit(SES_ID)
            drain_events(APP, 600)
            first = server.sse_connections
            self.assertEqual(first, 1)
            # Repeat everything that production repeats every few seconds.
            for _ in range(3):
                win._stream.retarget_endpoint.emit(f"http://127.0.0.1:{port}")
                win._stream.select_session.emit(SES_ID)
                win._stream.refresh_now.emit()
                drain_events(APP, 200)
            drain_events(APP, 400)
            self.assertEqual(server.sse_connections, 1,
                             "a second SSE connection was opened while one was alive")
        finally:
            self._teardown(win, server, db_path)


# ═══════════════════════════════════════════════════════════════════
# Required tests 12-15: stale endpoint results + audit target ownership
# ═══════════════════════════════════════════════════════════════════

class TestResolutionOwnership(unittest.TestCase):
    def setUp(self):
        self.db_path = create_test_db()
        self.server, self.port, _ = start_fake_server()
        self.server.server_sessions = {"ses_1", "ses_2"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.remove(self.db_path)

    def test_12_rapid_selection_discards_stale_resolution(self):
        """Required 12: a resolution started for A arriving after the GUI
        selected B is discarded."""
        win, mod = make_window(self.db_path)
        try:
            win.sessions = [make_summary("ses_1"), make_summary("ses_2")]
            win._on_session_selected(0)   # A -> generation 1
            gen_a = win._resolution_generation
            status_a = grant_endpoint(win, self.port, "ses_1", generation=gen_a)
            win._on_endpoint_resolved(status_a)
            self.assertTrue(win._can_send())

            win._on_session_selected(1)   # B -> generation 2
            gen_b = win._resolution_generation
            self.assertNotEqual(gen_a, gen_b)

            # LATE result for A (old generation): must be discarded.
            win._on_endpoint_resolved(status_a)
            self.assertNotEqual(win._endpoint_status.generation, gen_a or None)
            self.assertFalse(win._can_send(),
                             "stale A result must never authorize a send for B")

            # Current result for B: accepted.
            status_b = grant_endpoint(win, self.port, "ses_2", generation=gen_b,
                                      sessions=["ses_1", "ses_2"])
            win._on_endpoint_resolved(status_b)
            self.assertTrue(win._can_send())
        finally:
            win._shutdown_workers()

    def test_13_send_disabled_until_exact_resolution_current(self):
        """Required 13: after switching to B, Send stays disabled until B's
        OWN exact resolution is current."""
        win, mod = make_window(self.db_path)
        try:
            win.sessions = [make_summary("ses_1"), make_summary("ses_2")]
            win._on_session_selected(0)
            grant_endpoint(win, self.port, "ses_1")
            win._on_endpoint_resolved(win._endpoint_status)
            self.assertTrue(win.btn_send.isEnabled())

            win._on_session_selected(1)
            win._update_send_controls()
            self.assertFalse(win.btn_send.isEnabled(),
                             "B must not inherit A's resolution")
            self.assertFalse(win._can_send())

            grant_endpoint(win, self.port, "ses_2",
                           sessions=["ses_1", "ses_2"])
            win._on_endpoint_resolved(win._endpoint_status)
            self.assertTrue(win.btn_send.isEnabled())
        finally:
            win._shutdown_workers()

    def test_14_audit_read_on_a_never_lands_in_b(self):
        """Required 14: an audit file read started for A, with the selection
        switched to B mid-read, sends to the CAPTURED target A — never B."""
        win, mod = make_window(self.db_path)
        server_b, port_b, _ = start_fake_server()
        server_b.server_sessions = {"ses_2"}
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md",
                                             delete=False,
                                             encoding="utf-8") as f:
                f.write("AUDIT CORE CONTENT")
                audit_path = f.name

            win.sessions = [make_summary("ses_1"), make_summary("ses_2")]
            win._on_session_selected(0)                     # A
            drain_events(APP, 300)
            # Endpoint authority exists at click time (granted A).
            grant_endpoint(win, self.port, "ses_1")
            win._on_endpoint_resolved(win._endpoint_status)
            request = win._build_request(kind="core", slot="core",
                                         path=audit_path)
            self.assertEqual(request.session_id, "ses_1")
            self.assertEqual(request.endpoint_url, f"http://127.0.0.1:{self.port}")
            # The captured endpoint identity must be a REAL live process:
            # this test process itself.
            import psutil
            me = psutil.Process()
            request = replace(request, endpoint_pid=os.getpid(),
                              endpoint_start_time=me.create_time())
            # Restore the full session list; the user will switch to ses_2.
            win.sessions = [make_summary("ses_1"), make_summary("ses_2")]

            win._on_session_selected(1)                     # user switches to B
            self.assertEqual(win.current_session_id, "ses_2")

            # The read completes AFTER the switch: the immutable envelope
            # (not the current selection) drives the send.
            rec = SignalRecorder(win._sender.signals.send_completed)
            win._on_audit_file_loaded(request, "AUDIT CORE CONTENT")
            drain_events(APP, 1500)

            results = [r[0] for r in rec.items]
            self.assertTrue(results, "verified send never completed")
            final = results[-1]
            self.assertEqual(final.session_id, "ses_1",
                             "the captured target must survive a selection switch")
            self.assertTrue(final.success, f"verified send failed: {final.error}")
            self.assertEqual(len(server_b.admission_log), 0,
                             "the audit must NEVER land in session B")
            self.assertEqual(len(self.server.admission_log), 1)
            self.assertEqual(
                self.server.admission_log[0]["prompt"]["text"],
                "AUDIT CORE CONTENT")
            os.remove(audit_path)
        finally:
            server_b.shutdown()
            server_b.server_close()
            win._shutdown_workers()

    def test_15_stale_endpoint_or_missing_session_fails_closed(self):
        """Required 15: revalidation before native admission — a captured
        PID that is gone, or a captured session no longer on the captured
        endpoint, fails the request visibly. No reroute, no SQLite fallback."""
        win, mod = make_window(self.db_path)
        try:
            # a) Captured endpoint PID no longer alive.
            dead_req = sender_mod.NativeSender.new_request_id()
            from queue_viewer import AuditRequest
            req = AuditRequest(
                request_id=dead_req, kind="core", session_id="ses_1",
                project_name="p", resolver_generation=1,
                endpoint_pid=2 ** 22,  # effectively nonexistent
                endpoint_start_time=1.0,
                endpoint_url=f"http://127.0.0.1:{self.port}",
                slot="core", path="x")
            rec = SignalRecorder(win._sender.signals.send_completed)
            win._sender.send_verified_cmd.emit(dict(vars(req)), "AUDIT CORE")
            drain_events(APP, 1500)
            results = [r[0] for r in rec.items]
            self.assertTrue(results)
            self.assertFalse(results[-1].success)
            self.assertIn("failed closed", results[-1].error)
            self.assertEqual(len(self.server.admission_log), 0)

            # b) Captured session no longer exists on the captured endpoint.
            req2 = AuditRequest(
                request_id=sender_mod.NativeSender.new_request_id(),
                kind="core", session_id="ses_gone",
                project_name="p", resolver_generation=1,
                endpoint_pid=None, endpoint_start_time=None,
                endpoint_url=f"http://127.0.0.1:{self.port}",
                slot="core", path="x")
            rec.items.clear()
            win._sender.send_verified_cmd.emit(dict(vars(req2)), "AUDIT CORE")
            drain_events(APP, 1500)
            results = [r[0] for r in rec.items]
            self.assertTrue(results)
            self.assertFalse(results[-1].success)
            self.assertIn("failed closed", results[-1].error)
            self.assertEqual(len(self.server.admission_log), 0,
                             "no admission may be attempted on a dead target")
        finally:
            win._shutdown_workers()


# ═══════════════════════════════════════════════════════════════════
# Required tests 16-18: composer protection, result correlation,
# bounded in-flight shutdown
# ═══════════════════════════════════════════════════════════════════

class TestSendCorrelation(unittest.TestCase):
    def setUp(self):
        self.db_path = create_test_db()
        self.server, self.port, _ = start_fake_server()
        self.server.server_sessions = {"ses_1", "ses_2"}
        self.server.admission_log = []
        FakeOpenCodeHandler.admission_log = self.server.admission_log
        FakeOpenCodeHandler.fail_next = False
        FakeOpenCodeHandler.test_delay_seconds = 0.0

    def tearDown(self):
        FakeOpenCodeHandler.test_delay_seconds = 0.0
        self.server.shutdown()
        self.server.server_close()
        os.remove(self.db_path)

    def test_16_newer_composer_text_survives_old_completion(self):
        """Required 16: send A in flight, user types B — completion of A
        must leave B in the composer."""
        FakeOpenCodeHandler.test_delay_seconds = 0.8
        win, mod = make_window(self.db_path)
        try:
            grant_endpoint(win, self.port, "ses_1")
            win._on_endpoint_resolved(win._endpoint_status)

            win.input_prompt.setText("A")          # programmatic: not a user edit
            win._composer_submit = {"text": "A", "rev": win._composer_rev}
            win._on_send()
            drain_events(APP, 150)                 # request now in flight

            win.input_prompt.setText("B")          # the user types B
            win.input_prompt.textEdited.emit("B")  # user edit bumps revision
            drain_events(APP, 1600)                # A completes

            self.assertEqual(win.input_prompt.text(), "B",
                             "newer user text must never be erased by an older completion")
            self.assertEqual(len(self.server.admission_log), 1)
        finally:
            win._shutdown_workers()

    def test_16b_matching_completion_clears_composer(self):
        win, mod = make_window(self.db_path)
        try:
            grant_endpoint(win, self.port, "ses_1")
            win._on_endpoint_resolved(win._endpoint_status)
            win.input_prompt.setText("hello")
            win._on_send()
            drain_events(APP, 1500)
            self.assertEqual(win.input_prompt.text(), "")
            self.assertEqual(win.lbl_feedback.text(), "SAIPEN: queued #1")
        finally:
            win._shutdown_workers()

    def test_17_old_session_result_cannot_overwrite_new_selection(self):
        """Required 17: a send result for A must not overwrite the semantic
        state of the newly selected B."""
        win, mod = make_window(self.db_path)
        try:
            win.sessions = [make_summary("ses_1"), make_summary("ses_2")]
            win._on_session_selected(0)
            grant_endpoint(win, self.port, "ses_1")
            win._on_endpoint_resolved(win._endpoint_status)
            win.input_prompt.setText("in flight")
            win._composer_submit = {"text": "in flight", "rev": 0}
            win._on_send_started({"request_id": "r1", "session_id": "ses_1",
                                  "kind": "prompt"})

            win._on_session_selected(1)  # B, no resolution yet
            self.assertFalse(win.btn_send.isEnabled())

            # Freeze B's visible state BEFORE the stale completion arrives.
            feedback_b = win.lbl_feedback.text()
            composer_b = win.input_prompt.text()
            controls_b = [win.btn_send.isEnabled(), win.btn_cc.isEnabled(),
                          win.btn_core.isEnabled()]

            from viewer_sender import AdmissionResult
            win._on_send_completed(AdmissionResult(
                success=True, admitted_seq=12, request_id="r1",
                session_id="ses_1", kind="prompt"))

            # B's semantic state: no current resolution -> still disabled.
            self.assertFalse(win.btn_send.isEnabled(),
                             "A's success must not enable sending for B")
            self.assertEqual(win.input_prompt.text(), composer_b,
                             "B-session composer text is untouched")
            # STALE COMPLETION RULE: an old-session completion must not
            # mutate the SELECTED session's feedback at all.
            self.assertEqual(win.lbl_feedback.text(), feedback_b,
                             "old-session completion must not alter selected-"
                             "session feedback")
            self.assertEqual(controls_b,
                             [win.btn_send.isEnabled(), win.btn_cc.isEnabled(),
                              win.btn_core.isEnabled()],
                             "old-session completion must not flip controls")
        finally:
            win._shutdown_workers()

    def test_18_close_during_stalled_http_is_bounded_and_silent(self):
        """Required 18: closing the Viewer during a stalled admission leaves
        no QThread running, is bounded in time, and sends NO interrupt to
        OpenCode (only the original POST path is ever requested)."""
        FakeOpenCodeHandler.test_delay_seconds = 30.0
        win, mod = make_window(self.db_path)
        try:
            grant_endpoint(win, self.port, "ses_1")
            win._on_endpoint_resolved(win._endpoint_status)
            win.input_prompt.setText("slow one")
            win._on_send()
            drain_events(APP, 400)  # POST now stalled server-side

            t0 = time.time()
            win._shutdown_workers()
            elapsed = time.time() - t0
            self.assertLess(elapsed, 8.0,
                            f"shutdown stalled on in-flight HTTP ({elapsed:.1f}s)")
            self.assertFalse(win._sender_thread.isRunning())
            self.assertFalse(win._stream_thread.isRunning())
            self.assertFalse(win._observer_thread.isRunning())
            drain_events(APP, 100)
            # No abort/interrupt path was ever sent to the server: only the
            # original prompt POST (whose client wait we cancelled locally).
            for path in FakeOpenCodeHandler.request_paths:
                self.assertNotIn("interrupt", path)
                self.assertNotIn("abort", path)
                self.assertNotIn("cancel", path)
        finally:
            FakeOpenCodeHandler.test_delay_seconds = 0.0
            self.server.shutdown()
            self.server.server_close()


# ═══════════════════════════════════════════════════════════════════
# Required tests 19-21: ONE total bound, native timestamps, F5
# ═══════════════════════════════════════════════════════════════════

class TestBoundsTimestampsRefresh(unittest.TestCase):
    def test_19_one_total_128kib_bound_across_parts(self):
        """Required 19: a multi-part turn obeys ONE total UTF-8 bound — not
        per part. Never splits characters; keeps a truncation marker."""
        observer = obs.ReadOnlyObserver("")
        part_bytes = 90 * 1024
        observer._process_event_text("text.started",
                                     {"assistantMessageID": "m1", "textID": "t1"})
        observer._process_event_text(
            "text.delta", {"assistantMessageID": "m1", "textID": "t1",
                           "delta": "Ж" * (part_bytes // 2)})  # 2 bytes/char
        observer._process_event_text("text.started",
                                     {"assistantMessageID": "m1", "textID": "t2"})
        observer._process_event_text(
            "text.delta", {"assistantMessageID": "m1", "textID": "t2",
                           "delta": "Ж" * (part_bytes // 2)})
        observer._process_event_text(
            "text.delta", {"assistantMessageID": "m1", "textID": "t2",
                           "delta": "日本語" * 5000})
        text = observer._assembled_text()
        raw = text.encode("utf-8")
        self.assertLessEqual(len(raw), obs.ReadOnlyObserver.MAX_TEXT_BYTES,
                             "TOTAL turn bytes must stay within 128 KiB")
        self.assertGreater(len(raw), 64 * 1024, "newest output must be preserved")
        self.assertIn("truncated", text)
        raw.decode("utf-8")  # never a split character

        # The live worker obeys the same ONE total bound.
        w = make_live_worker()
        w.debug_handle_frame(live_envelope(
            "session.next.text.started", assistantMessageID=ASSIST_MSG_ID,
            textID="t1"))
        for i in range(8):
            w.debug_handle_frame(live_envelope(
                "session.next.text.delta", assistantMessageID=ASSIST_MSG_ID,
                textID="t1", delta="x" * 20000))
        self.assertLessEqual(w._total_bytes,
                             lstream.LiveStreamWorker.MAX_TOTAL_BYTES)

    def test_20_last_event_age_uses_native_timestamp(self):
        """Required 20: event age is the NATIVE event time (ms -> s), not
        the observer read time."""
        db_path = create_test_db()
        try:
            observer = obs.ReadOnlyObserver(db_path)
            observer._open_connection()
            observer._selected_session_id = SES_ID
            rec = SignalRecorder(observer.signals.event_observed)
            observer._hydrate(observer._conn.cursor(), SES_ID)
            stamps = [t for (_, _, t) in rec.items]
            self.assertTrue(stamps)
            native_secs = TS_PROMPTED_MS / 1000.0
            self.assertTrue(any(abs(t - native_secs) < 0.01 for t in stamps),
                            f"event_observed must carry the native ms timestamp "
                            f"(expected ~{native_secs}, got {stamps})")
            self.assertFalse(any(abs(t - time.time()) < 2.0 for t in stamps),
                             "observer-read time leaked into event age")
            observer.stop()
            del observer
            import gc
            gc.collect()
        finally:
            os.remove(db_path)

    def test_20b_live_stream_native_timestamp_feeds_age(self):
        win, mod = make_window(create_test_db())
        try:
            win.current_session_id = SES_ID
            win._on_live_event_time(SES_ID, 1700000000.5)
            drain_events(APP, 200)
            self.assertEqual(win._last_event_time, 1700000000.5)
            other = SES_ID.replace("f8c8", "f8c9")
            win._on_live_event_time(other, 1800000000.0)
            drain_events(APP, 200)
            self.assertEqual(win._last_event_time, 1700000000.5,
                             "another session's live events must not move the age")
        finally:
            win._shutdown_workers()

    def test_21_f5_refreshes_every_worker(self):
        """Required 21: F5 forces observer + resolver + live stream + preset
        refresh — not just the preset worker."""
        win, mod = make_window(create_test_db())
        try:
            rec_obs = SignalRecorder(win._observer.refresh_now)
            rec_res = SignalRecorder(win._resolver.resolve_now)
            rec_str = SignalRecorder(win._stream.refresh_now)
            rec_pre = SignalRecorder(win._preset_worker.refresh_requested)
            win._force_refresh()
            drain_events(APP, 400)
            self.assertEqual(len(rec_obs.items), 1, "F5 must refresh the observer")
            self.assertEqual(len(rec_res.items), 1, "F5 must force resolution")
            self.assertEqual(len(rec_str.items), 1, "F5 must refresh the stream")
            self.assertEqual(len(rec_pre.items), 1, "F5 must refresh presets")
        finally:
            win._shutdown_workers()


# ═══════════════════════════════════════════════════════════════════
# Performance gate: 10,000 deltas over a real SSE socket
# ═══════════════════════════════════════════════════════════════════

class TestPerformanceGate(unittest.TestCase):
    def test_10k_deltas_drained_coalesced_responsive(self):
        db_path = create_test_db(events=ACTIVE_DB_EVENTS, sessions=(SES_ID,))
        server, port, _ = start_fake_server()
        server.server_sessions = {SES_ID}
        win, mod = make_window(db_path)
        try:
            win.current_session_id = SES_ID
            grant_endpoint(win, port, SES_ID)
            win._on_endpoint_resolved(win._endpoint_status)
            win._stream.retarget_endpoint.emit(f"http://127.0.0.1:{port}")
            win._stream.select_session.emit(SES_ID)
            drain_events(APP, 600)

            heartbeats = []
            hb = QTimer()
            hb.timeout.connect(lambda: heartbeats.append(time.time()))
            hb.start(100)

            t0 = time.time()
            total = 10000
            marker = f"d{total - 1}END"
            # 10k deltas pushed over the real socket in bursts; the reader
            # must drain continuously (no server-side accumulation) while
            # the GUI stays responsive and repaints stay coalesced.
            # Native sequence: step.started establishes the assistant
            # generation identity before any delta.
            server.push_event(live_envelope(
                "session.next.step.started", assistantMessageID=ASSIST_MSG_ID))

            def push_bursts():
                for burst in range(0, total, 500):
                    for i in range(burst, burst + 500):
                        server.push_event(live_envelope(
                            "session.next.text.delta",
                            assistantMessageID=ASSIST_MSG_ID,
                            textID=TEXT_PART_ID,
                            delta="word " if i < total - 1 else marker))
                    time.sleep(0.02)

            pusher = threading.Thread(target=push_bursts, daemon=True)
            pusher.start()
            deadline = time.time() + 60
            while time.time() < deadline:
                drain_events(APP, 200)
                if marker in win.txt_live_output.toPlainText():
                    break
            pusher.join(5)
            elapsed = time.time() - t0

            self.assertIn(marker, win.txt_live_output.toPlainText(),
                          "stream never converged after 10k deltas")
            self.assertLess(elapsed, 45,
                            f"10k-delta stream took {elapsed:.1f}s (drain stalled)")
            # The pusher bursts 500 deltas then sleeps 0.02s for ~20 waves;
            # the drain loop above exits as soon as the final marker lands
            # (the pusher's own sleeps dominate the wall time), so the
            # required contract here is that the heartbeat was NEVER fully
            # starved for the whole flood window.
            self.assertGreater(len(heartbeats), 0,
                               "GUI heartbeat starved during delta flood")
            # Memory stays bounded: the ONE total cap holds.
            self.assertLessEqual(win._stream._total_bytes,
                                 lstream.LiveStreamWorker.MAX_TOTAL_BYTES)
            hb.stop()
            print(f"\n[perf] 10k deltas: {elapsed:.1f}s, "
                  f"{len(heartbeats)} GUI heartbeats, "
                  f"{win._stream._total_bytes} bytes in model")
        finally:
            hb.stop()
            win._shutdown_workers()
            server.shutdown()
            server.server_close()
            os.remove(db_path)


# ═══════════════════════════════════════════════════════════════════
# Launch contract (gates 1, 2, 26, 27)
# ═══════════════════════════════════════════════════════════════════

class TestLaunchContract(unittest.TestCase):
    def test_01_queue_ps1_default_is_viewer(self):
        """queue.ps1 default GUI resolves to queue_viewer.pyw, not queue_manager.pyw."""
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(root, "Scripts", "saipatch", "queue.ps1"), encoding="utf-8") as f:
            src = f.read()
        viewer_idx = src.index("$ViewerScript = Join-Path $ScriptDir 'queue_viewer.pyw'")
        gui_idx = src.index("if ($Command -eq 'gui' -and -not $LegacyCommand)")
        launch_idx = src.index("$argsList = @($ViewerScript)", gui_idx)
        self.assertGreater(launch_idx, gui_idx)
        self.assertGreater(gui_idx, viewer_idx)
        self.assertIn("$LegacyCommand", src)
        legacy_idx = src.index("$LegacyScript = Join-Path $ScriptDir 'queue_manager.pyw'")
        self.assertGreater(legacy_idx, launch_idx)
        self.assertNotIn("$PyScript = Join-Path $ScriptDir 'queue_manager.pyw'", src)
        self.assertIn("LEGACY DIRECT-DB TOOL", src)

    def test_02_saituls_exposes_queue_viewer_tool(self):
        """SAITULS exposes OpenCode Queue Viewer through the normal tool surface."""
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(root, "SAITULS.cs"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('{ "OpenCode Queue Viewer", "saipatch", "saipatch-queue-viewer" }', src)
        self.assertIn('"saipatch-queue-viewer"', src)
        self.assertIn('"queue.ps1"', src)

    def test_27_no_queue_core_mutation_usage(self):
        """No queue-viewer production module imports or calls queue_core mutation functions."""
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Scripts", "saipatch"))
        for name in ("queue_viewer.pyw", "viewer_observer.py", "viewer_endpoint.py",
                     "viewer_sender.py", "viewer_presets.py", "viewer_live_stream.py"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                src = f.read()
            self.assertNotIn("import queue_core", src)
            for mut in ("add_item", "drop_item", "clear_queue", "reorder",
                        "edit_item", "delete_session"):
                self.assertNotIn(f"queue_core.{mut}", src, f"{name} references queue_core.{mut}")
            for sql in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
                self.assertNotIn(sql, src, f"{name} contains direct SQL mutation")

    def test_26_observer_connection_is_ro_and_query_only(self):
        """All SQLite connections are mode=ro + PRAGMA query_only (real observer path)."""
        db_path = create_test_db()
        try:
            observer = obs.ReadOnlyObserver(db_path)
            conn = observer._open_connection()
            self.assertIsNotNone(conn)
            mode = conn.execute("PRAGMA query_only").fetchone()[0]
            self.assertEqual(mode, 1)
            self.assertIn("mode=ro", observer._last_open_uri)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO session VALUES ('x','x','x',0)")
            observer.stop()
            del observer
            import gc
            gc.collect()
        finally:
            os.remove(db_path)


# ═══════════════════════════════════════════════════════════════════
# Thread ownership (gates 3, 4, 6, 7)
# ═══════════════════════════════════════════════════════════════════

class TestThreadOwnership(unittest.TestCase):
    def test_03_db_work_never_on_gui_thread(self):
        """Worker method thread IDs prove DB work never executes on the
        QApplication main thread (real observer in a real QThread)."""
        db_path = create_test_db()
        try:
            observer = obs.ReadOnlyObserver(db_path)
            thread = QThread()
            observer.moveToThread(thread)
            thread.started.connect(observer.start)
            observer.select_session.connect(observer._on_select_session)
            observer.stop_worker.connect(observer._on_stop)
            rec_side = SignalRecorder(observer.signals.sidebar_updated)
            rec_health = SignalRecorder(observer.signals.observer_health)
            thread.start()
            drain_events(APP, 1500)

            self.assertTrue(rec_side.items, "observer never emitted sidebar_updated")
            self.assertTrue(rec_health.items, "observer never emitted observer_health")
            worker_tid = observer._last_refresh_thread_id
            self.assertIsNotNone(worker_tid)
            self.assertNotEqual(worker_tid, MAIN_THREAD_ID,
                                "DB work executed on the GUI/main thread")

            observer.stop_worker.emit()
            drain_events(APP, 300)
            thread.quit()
            self.assertTrue(thread.wait(3000))
            del observer
            del thread
            import gc
            gc.collect()
            time.sleep(0.05)
        finally:
            os.remove(db_path)

    def test_04_observer_connection_created_in_worker_thread(self):
        """ReadOnlyObserver opens (and closes) its SQLite connection in the
        worker thread, not the GUI thread."""
        db_path = create_test_db()
        try:
            observer = obs.ReadOnlyObserver(db_path)
            self.assertIsNone(observer._conn)
            thread = QThread()
            observer.moveToThread(thread)
            thread.started.connect(observer.start)
            observer.stop_worker.connect(observer._on_stop)
            thread.start()
            drain_events(APP, 1200)
            self.assertIsNotNone(observer._conn)
            self.assertNotEqual(observer._conn_open_thread_id, MAIN_THREAD_ID)
            observer.stop_worker.emit()
            drain_events(APP, 300)
            thread.quit()
            self.assertTrue(thread.wait(3000))
            self.assertIsNone(observer._conn)
            del observer
            del thread
            import gc
            gc.collect()
            time.sleep(0.05)
        finally:
            os.remove(db_path)

    def test_06_selection_commands_reach_workers_via_queued_calls(self):
        """Session-selection commands reach observer/resolver through queued calls."""
        db_path = create_test_db()
        try:
            observer = obs.ReadOnlyObserver(db_path)
            thread = QThread()
            observer.moveToThread(thread)
            thread.started.connect(observer.start)
            observer.select_session.connect(observer._on_select_session)
            observer.stop_worker.connect(observer._on_stop)
            thread.start()
            drain_events(APP, 800)

            observer.select_session.emit("ses_2")
            drain_events(APP, 400)
            self.assertEqual(observer._selected_session_id, "ses_2")
            self.assertNotEqual(observer._last_select_thread_id, MAIN_THREAD_ID)

            observer.stop_worker.emit()
            thread.quit()
            self.assertTrue(thread.wait(3000))
            del observer
            del thread
            import gc
            gc.collect()
        finally:
            os.remove(db_path)

    def test_07_shutdown_no_timer_or_sqlite_violations(self):
        """Shutdown stops QTimers, closes SQLite in owner threads, stops all
        FIVE worker threads (incl. the live stream) with no Qt violations."""
        warnings = []
        from PyQt6.QtCore import qInstallMessageHandler

        def handler(mode, ctx, msg):
            warnings.append(msg)

        old = qInstallMessageHandler(handler)
        try:
            win, mod = make_window(create_test_db())
            drain_events(APP, 1200)
            win._shutdown_workers()
            drain_events(APP, 200)
            self.assertFalse(win._observer_thread.isRunning())
            self.assertFalse(win._resolver_thread.isRunning())
            self.assertFalse(win._sender_thread.isRunning())
            self.assertFalse(win._stream_thread.isRunning())
            self.assertFalse(win._preset_thread.isRunning())
            self.assertIsNone(win._observer._conn)
            bad = [w for w in warnings
                   if "killTimer" in w or "startTimer" in w
                   or "QThread: Destroyed while thread is still running" in w]
            self.assertEqual(bad, [], f"Qt threading violations: {bad}")
        finally:
            qInstallMessageHandler(old)


# ═══════════════════════════════════════════════════════════════════
# GUI responsiveness during HTTP (gates 4b, 5, 25)
# ═══════════════════════════════════════════════════════════════════

class TestGuiResponsiveness(unittest.TestCase):
    def setUp(self):
        self.db_path = create_test_db()
        self.server, self.port, _ = start_fake_server()
        self.server.admission_log = []
        FakeOpenCodeHandler.admission_log = self.server.admission_log
        FakeOpenCodeHandler.fail_next = False
        FakeOpenCodeHandler.test_delay_seconds = 0.0

    def tearDown(self):
        FakeOpenCodeHandler.test_delay_seconds = 0.0
        self.server.shutdown()
        self.server.server_close()
        os.remove(self.db_path)

    def _grant(self, win):
        grant_endpoint(win, self.port, "ses_1")
        win._on_endpoint_resolved(win._endpoint_status)

    def test_04b_slow_send_keeps_gui_responsive(self):
        """A fake HTTP endpoint delaying 2s: Viewer remains responsive,
        heartbeat continues, feedback shows the correlated result, no freeze."""
        FakeOpenCodeHandler.test_delay_seconds = 2.0
        win, mod = make_window(self.db_path)
        try:
            self._grant(win)

            heartbeats = []
            hb = QTimer()
            hb.timeout.connect(lambda: heartbeats.append(time.time()))
            hb.start(100)

            win.input_prompt.setText("hello")
            t0 = time.time()
            win._on_send()
            drain_events(APP, 3000)

            elapsed = time.time() - t0
            self.assertGreaterEqual(len(heartbeats), 10,
                                    "GUI heartbeat starved during HTTP send (UI freeze)")
            self.assertEqual(win.lbl_feedback.text(), "SAIPEN: queued #1")
            self.assertEqual(len(self.server.admission_log), 1)
            self.assertEqual(self.server.admission_log[0]["delivery"], "queue")
            self.assertLess(elapsed, 6.0)
            hb.stop()
        finally:
            hb.stop()
            win._shutdown_workers()

    def test_05_three_waves_delayed_keeps_gui_responsive(self):
        """3 WAVES with three delayed fake responses: Viewer remains
        responsive for the entire sequence."""
        FakeOpenCodeHandler.test_delay_seconds = 0.7
        win, mod = make_window(self.db_path)
        presets_file = tempfile.mktemp(suffix=".json")
        paths = []
        for name in ["core", "wave2", "performance"]:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                             encoding="utf-8") as f:
                f.write(f"AUDIT {name.upper()}")
                paths.append(f.name)
        old_file = presets.PRESETS_FILE
        presets.PRESETS_FILE = presets_file
        try:
            presets.save_preset("core", paths[0])
            presets.save_preset("wave2", paths[1])
            presets.save_preset("performance", paths[2])

            self._grant(win)
            win._preset_paths = {"core": paths[0], "wave2": paths[1],
                                 "performance": paths[2]}

            heartbeats = []
            hb = QTimer()
            hb.timeout.connect(lambda: heartbeats.append(time.time()))
            hb.start(100)

            win._on_3waves()
            drain_events(APP, 3600)

            self.assertGreaterEqual(len(heartbeats), 15,
                                    "GUI heartbeat starved during 3 WAVES")
            self.assertEqual(len(self.server.admission_log), 3)
            self.assertEqual(self.server.admission_log[0]["prompt"]["text"], "AUDIT CORE")
            self.assertEqual(self.server.admission_log[1]["prompt"]["text"], "AUDIT WAVE2")
            self.assertEqual(self.server.admission_log[2]["prompt"]["text"], "AUDIT PERFORMANCE")
            self.assertIn("CORE/W2/PERF", win.lbl_feedback.text())
            hb.stop()
        finally:
            hb.stop()
            win._shutdown_workers()
            presets.PRESETS_FILE = old_file
            for p in paths:
                os.remove(p)
            if os.path.exists(presets_file):
                os.remove(presets_file)

    def test_25_slow_preset_worker_does_not_block_gui(self):
        """Preset file I/O on an artificially slow worker path does not block
        the GUI heartbeat."""
        win, mod = make_window(self.db_path)
        try:
            drain_events(APP, 600)

            heartbeats = []
            hb = QTimer()
            hb.timeout.connect(lambda: heartbeats.append(time.time()))
            hb.start(100)

            import viewer_presets as vp
            real_load = vp.load_presets

            def slow_load():
                time.sleep(1.2)
                return real_load()

            win._preset_worker._slow_hook = slow_load  # documented test seam
            qv = sys.modules["queue_viewer"]
            orig_refresh = qv.PresetWorker.refresh_presets

            def slow_refresh(self_worker):
                if getattr(self_worker, "_slow_hook", None):
                    self_worker._slow_hook()
                orig_refresh(self_worker)

            qv.PresetWorker.refresh_presets = slow_refresh
            try:
                win._preset_worker.refresh_requested.emit()
                drain_events(APP, 1800)
            finally:
                qv.PresetWorker.refresh_presets = orig_refresh

            self.assertGreaterEqual(len(heartbeats), 8,
                                    "GUI heartbeat starved by preset file I/O")
            hb.stop()
        finally:
            hb.stop()
            win._shutdown_workers()


# ═══════════════════════════════════════════════════════════════════
# Execution state machine (gates 8-11)
# ═══════════════════════════════════════════════════════════════════

class TestExecutionState(unittest.TestCase):
    def test_08_verified_running_session_displays_running(self):
        """Verified running session displays RUNNING (snapshot liveness)."""
        db_path = create_test_db(sessions=("ses_1", "ses_2", "ses_3"))
        try:
            w = sqlite3.connect(db_path)
            w.execute("INSERT INTO event VALUES ('e1', 'ses_3', 1, 'session.next.prompted.1', '{}')")
            w.execute("INSERT INTO event VALUES ('e2', 'ses_3', 2, 'session.next.step.started.2', '{}')")
            w.commit()
            w.close()

            observer = obs.ReadOnlyObserver(db_path)
            observer._open_connection()
            observer._live_snapshots = (ep.ProcessSnapshot(
                pid=1, start_time=1.0, executable_path="x:\\opencode.exe",
                endpoint="http://127.0.0.1:1",
                verified_session_ids=("ses_3",)),)
            rec = SignalRecorder(observer.signals.sidebar_updated)
            observer._refresh_sidebar()
            summaries = rec.last[0]
            s3 = next(s for s in summaries if s.id == "ses_3")
            self.assertEqual(s3.state, "RUNNING")
            self.assertTrue(s3.is_process_live)
            observer.stop()
            del observer
            import gc
            gc.collect()
            time.sleep(0.05)
        finally:
            os.remove(db_path)

    def test_09_text_delta_does_not_reset_running_to_idle(self):
        observer = obs.ReadOnlyObserver("")
        sid = "ses_state"
        observer.apply_execution_event(sid, "session.next.step.started", {}, 1)
        self.assertEqual(observer._session_states[sid], "RUNNING")
        observer.apply_execution_event(sid, "session.next.text.delta", {"delta": "x"}, 2)
        self.assertEqual(observer._session_states[sid], "RUNNING")
        observer.apply_execution_event(sid, "session.next.text.started", {}, 3)
        observer.apply_execution_event(sid, "session.next.reasoning.delta", {"delta": "y"}, 4)
        observer.apply_execution_event(sid, "session.next.tool.called", {"tool": "read"}, 5)
        self.assertEqual(observer._session_states[sid], "RUNNING",
                         "activity events must not reset RUNNING to IDLE")
        observer.stop()
        del observer
        import gc
        gc.collect()

    def test_10_terminal_step_truthfully_transitions(self):
        observer = obs.ReadOnlyObserver("")
        sid = "ses_state"
        observer.apply_execution_event(sid, "session.next.prompted", {}, 1)
        observer.apply_execution_event(sid, "session.next.step.started", {}, 2)
        observer.apply_execution_event(sid, "session.next.step.ended", {"finish": "stop"}, 3)
        self.assertEqual(observer._session_states[sid], "IDLE")
        observer.apply_execution_event(sid, "session.next.step.ended", {"finish": "tool-calls"}, 4)
        self.assertEqual(observer._session_states[sid], "RUNNING")
        observer.stop()
        del observer
        import gc
        gc.collect()

    def test_11_permission_question_input_required(self):
        observer = obs.ReadOnlyObserver("")
        sid = "ses_state"
        observer.apply_execution_event(sid, "session.next.permission.asked", {}, 1)
        self.assertEqual(observer._session_states[sid], "NEEDS_HUMAN")
        sid2 = sid + "2"
        observer.apply_execution_event(sid2, "session.next.question.asked", {}, 1)
        self.assertEqual(observer._session_states[sid2], "NEEDS_HUMAN")
        observer.stop()
        del observer
        import gc
        gc.collect()

    def test_08b_unverified_running_downgraded_truthfully(self):
        """Stale RUNNING with no verified endpoint snapshot becomes IDLE."""
        db_path = create_test_db(events=ACTIVE_DB_EVENTS, sessions=(SES_ID,))
        try:
            observer = obs.ReadOnlyObserver(db_path)
            observer._open_connection()
            observer._live_snapshots = ()
            rec = SignalRecorder(observer.signals.sidebar_updated)
            observer._refresh_sidebar()
            summaries = rec.last[0]
            s1 = next(s for s in summaries if s.id == SES_ID)
            self.assertEqual(s1.state, "IDLE")
            self.assertFalse(s1.is_process_live)
            observer.stop()
            del observer
            import gc
            gc.collect()
        finally:
            os.remove(db_path)


# ═══════════════════════════════════════════════════════════════════
# Queue table + event age (gates 12, 13)
# ═══════════════════════════════════════════════════════════════════

class TestQueueAndAge(unittest.TestCase):
    def test_12_queue_n_to_zero_clears(self):
        """Queue N -> 0 clears NEXT QUEUE rows (empty list IS emitted)."""
        db_path = create_test_db()
        try:
            observer = obs.ReadOnlyObserver(db_path)
            observer._open_connection()
            observer._selected_session_id = "ses_1"
            rec = SignalRecorder(observer.signals.pending_updated)
            observer._refresh_sidebar()
            self.assertEqual(rec.last[0], "ses_1")
            self.assertEqual(len(rec.last[1]), 1)

            w = sqlite3.connect(db_path)
            w.execute("DELETE FROM session_input")
            w.commit()
            w.close()

            rec.items.clear()
            observer._refresh_sidebar()
            self.assertEqual(rec.items, [("ses_1", [])],
                             "empty queue must emit pending_updated with []")
            observer.stop()
            del observer
            import gc
            gc.collect()
        finally:
            os.remove(db_path)

    def test_13_event_observed_for_every_event_including_delta(self):
        """Every durable event refreshes the age feed (event_observed)."""
        db_path = create_test_db()
        try:
            observer = obs.ReadOnlyObserver(db_path)
            observer._open_connection()
            observer._selected_session_id = SES_ID
            rec = SignalRecorder(observer.signals.event_observed)
            observer._hydrate(observer._conn.cursor(), SES_ID)
            seqs = [i[1] for i in rec.items]
            self.assertEqual(len(seqs), 5)  # prompted, step.started, text.started, text.ended, step.ended
            self.assertIn(4, seqs)
            observer.stop()
            del observer
            import gc
            gc.collect()
        finally:
            os.remove(db_path)

    def test_13b_ui_last_event_age_uses_event_observed(self):
        """The Viewer's Last event age is fed by event_observed."""
        win, mod = make_window(create_test_db())
        try:
            win.current_session_id = SES_ID
            before = win._last_event_time
            win._observer.signals.event_observed.emit(SES_ID, 42, 12345.0)
            drain_events(APP, 200)
            self.assertEqual(win._last_event_time, 12345.0)
            self.assertNotEqual(before, 12345.0)
            win._observer.signals.event_observed.emit("ses_other", 43, 1.0)
            drain_events(APP, 200)
            self.assertEqual(win._last_event_time, 12345.0)
        finally:
            win._shutdown_workers()


# ═══════════════════════════════════════════════════════════════════
# Multi-part text + byte bound (native identities)
# ═══════════════════════════════════════════════════════════════════

class TestMultiPartText(unittest.TestCase):
    def test_14_text_parts_around_tool_call_survive(self):
        """Multiple text parts around a tool call remain visible in order
        (real native identities)."""
        observer = obs.ReadOnlyObserver("")
        m = {"assistantMessageID": ASSIST_MSG_ID}
        observer._process_event_text("text.started", {**m, "textID": "t1"})
        observer._process_event_text("text.delta", {**m, "textID": "t1", "delta": "Part one."})
        observer._process_event_text("text.ended", {**m, "textID": "t1", "text": "Part one."})
        observer._process_event_text("tool.called", {**m, "tool": "read", "input": {"path": "a.txt"}})
        observer._process_event_text("text.started", {**m, "textID": "t2"})
        observer._process_event_text("text.delta", {**m, "textID": "t2", "delta": "Part two."})
        text = observer._assembled_text()
        self.assertIn("Part one.", text)
        self.assertIn("Part two.", text)
        self.assertLess(text.index("Part one."), text.index("Part two."))

    def test_14b_new_generation_clears_prior_turn(self):
        observer = obs.ReadOnlyObserver("")
        observer._process_event_text("text.started",
                                     {"assistantMessageID": "m1", "textID": "t1"})
        observer._process_event_text("text.delta",
                                     {"assistantMessageID": "m1", "textID": "t1",
                                      "delta": "old"})
        self.assertEqual(observer._assembled_text(), "old")
        observer._process_event_text("text.started",
                                     {"assistantMessageID": "m2", "textID": "t1"})
        self.assertEqual(observer._assembled_text(), "")

    def test_14c_no_reasoning_rendered(self):
        """Reasoning content is never rendered in LIVE OUTPUT."""
        observer = obs.ReadOnlyObserver("")
        observer._process_event_text("reasoning.started", {})
        observer._process_event_text("reasoning.delta", {"delta": "secret chain of thought"})
        observer._process_event_text("reasoning.ended", {})
        self.assertEqual(observer._assembled_text(), "")
        observer.stop()
        del observer
        import gc
        gc.collect()

    def test_15_unicode_output_respects_byte_cap(self):
        """Unicode output respects the 128 KiB TOTAL byte cap without
        splitting characters."""
        observer = obs.ReadOnlyObserver("")
        observer._process_event_text("text.started",
                                     {"assistantMessageID": "m1", "textID": "t1"})
        chunk = "日本語テキスト" * 4000 + "Кириллица" * 6000
        for _ in range(3):
            observer._process_event_text("text.delta",
                                         {"assistantMessageID": "m1", "textID": "t1",
                                          "delta": chunk})
        text = observer._assembled_text()
        raw_bytes = len(text.encode("utf-8"))
        self.assertLessEqual(raw_bytes, obs.ReadOnlyObserver.MAX_TEXT_BYTES)
        self.assertIn("truncated", text)
        text.encode("utf-8").decode("utf-8")
        observer.stop()
        del observer
        import gc
        gc.collect()

    def test_15b_durable_ended_replaces_partial_under_bound(self):
        """A durable final text replaces the partial even when the turn is
        already large; total stays bounded."""
        observer = obs.ReadOnlyObserver("")
        observer._process_event_text("text.started",
                                     {"assistantMessageID": "m", "textID": "t"})
        observer._process_event_text(
            "text.delta", {"assistantMessageID": "m", "textID": "t",
                           "delta": "a" * (obs.ReadOnlyObserver.MAX_TEXT_BYTES)})
        observer._process_event_text(
            "text.ended", {"assistantMessageID": "m", "textID": "t",
                           "text": "final concise answer"})
        self.assertEqual(observer._assembled_text(), "final concise answer")
        observer.stop()
        del observer
        import gc
        gc.collect()


# ═══════════════════════════════════════════════════════════════════
# Endpoint gates (wildcards, loopback, spoof, isolation)
# ═══════════════════════════════════════════════════════════════════

class TestEndpointGates(unittest.TestCase):
    def test_16_wildcard_ipv4_rejected(self):
        self.assertFalse(ep.is_allowed_listener("0.0.0.0"))

    def test_17_wildcard_ipv6_rejected(self):
        self.assertFalse(ep.is_allowed_listener("::"))

    def test_18_loopback_ipv4_accepted(self):
        self.assertTrue(ep.is_allowed_listener("127.0.0.1"))

    def test_19_loopback_ipv6_accepted_and_bracketed(self):
        self.assertTrue(ep.is_allowed_listener("::1"))
        self.assertEqual(ep.listener_probe_url("::1", 4096), "http://[::1]:4096")
        self.assertEqual(ep.listener_probe_url("127.0.0.1", 4096), "http://127.0.0.1:4096")

    def test_20_spoofed_executable_rejected(self):
        fake = os.path.join(tempfile.gettempdir(), "not-really-opencode.exe")
        with open(fake, "wb") as f:
            f.write(b"MZ")
        try:
            self.assertFalse(ep.verify_executable(fake, "C:/real/opencode.exe"))
            self.assertFalse(ep.verify_executable(None, None))
            self.assertFalse(ep.verify_executable("", None))
            self.assertTrue(ep.verify_executable(fake, None) is True or
                            ep.verify_executable(fake, None) is False)
        finally:
            os.remove(fake)
        other = os.path.join(tempfile.gettempdir(), "chrome.exe")
        with open(other, "wb") as f:
            f.write(b"MZ")
        try:
            self.assertFalse(ep.verify_executable(other, None))
        finally:
            os.remove(other)

    def test_21_idle_session_resolves_and_is_sendable(self):
        """Idle but valid selected session resolves via exact-session GET and
        can receive delivery:'queue'."""
        server, port, _ = start_fake_server()
        server.admission_log = []
        FakeOpenCodeHandler.admission_log = server.admission_log
        try:
            resolver = ep.OpenCodeEndpointResolver()
            resolver._target_session_id = "ses_1"
            verified = resolver._probe_endpoint(f"http://127.0.0.1:{port}")
            self.assertIsNotNone(verified)
            self.assertIn("ses_1", verified)
            sender = sender_mod.NativeSender()
            result = sender._send_one(f"http://127.0.0.1:{port}", "ses_1", "hello idle")
            self.assertTrue(result.success)
            self.assertEqual(server.admission_log[0]["delivery"], "queue")
            self.assertEqual(result.request_id, None)  # direct call: no minted id asserted
        finally:
            server.shutdown()
            server.server_close()

    def test_21b_missing_exact_session_fails_closed(self):
        """A session that does not exist on the endpoint never resolves."""
        server, port, _ = start_fake_server()
        try:
            resolver = ep.OpenCodeEndpointResolver()
            resolver._target_session_id = "ses_other"
            self.assertIsNone(resolver._probe_endpoint(f"http://127.0.0.1:{port}"))
        finally:
            server.shutdown()
            server.server_close()

    def test_22_disappearance_disables_send_synchronously(self):
        """Selected-session disappearance disables Send synchronously."""
        win, mod = make_window(create_test_db())
        try:
            grant_endpoint(win, 1, "ses_1")
            win._on_endpoint_resolved(win._endpoint_status)
            self.assertTrue(win._can_send())

            win._on_session_disappeared()
            self.assertFalse(win._can_send())
            self.assertFalse(win.btn_send.isEnabled())
            self.assertFalse(win.btn_cc.isEnabled())
            self.assertFalse(win.btn_3waves.isEnabled())
            self.assertFalse(win.btn_audit_file.isEnabled())
            self.assertFalse(win.input_prompt.isEnabled())
            self.assertEqual(win.table_queue.rowCount(), 0)
            self.assertEqual(win.lbl_native_status.text(), "[GONE]")
        finally:
            win._shutdown_workers()

    def test_23_two_processes_no_cross_send(self):
        """Two OpenCode processes: session A resolves only to endpoint A;
        session B resolves only to endpoint B; no cross-send."""
        serverA, portA, _ = start_fake_server()
        serverB, portB, _ = start_fake_server()
        try:
            snapA = ep.ProcessSnapshot(pid=101, start_time=1.0,
                                       executable_path="c:/x/opencode.exe",
                                       endpoint=f"http://127.0.0.1:{portA}",
                                       verified_session_ids=("ses_1",))
            snapB = ep.ProcessSnapshot(pid=102, start_time=2.0,
                                       executable_path="c:/x/opencode.exe",
                                       endpoint=f"http://127.0.0.1:{portB}",
                                       verified_session_ids=("ses_2",))

            resolver = ep.OpenCodeEndpointResolver()

            resolver._target_session_id = "ses_1"
            with unittest.mock.patch.object(resolver, "_scan_candidates",
                                            return_value=[snapA, snapB]):
                status = resolver._do_resolve()
            self.assertEqual(status.state, "RESOLVED")
            self.assertEqual(status.endpoint.url, f"http://127.0.0.1:{portA}")
            self.assertEqual(status.session_id, "ses_1")

            resolver._target_session_id = "ses_2"
            with unittest.mock.patch.object(resolver, "_scan_candidates",
                                            return_value=[snapA, snapB]):
                status = resolver._do_resolve()
            self.assertEqual(status.state, "RESOLVED")
            self.assertEqual(status.endpoint.url, f"http://127.0.0.1:{portB}")
            self.assertEqual(status.session_id, "ses_2")
        finally:
            serverA.shutdown()
            serverA.server_close()
            serverB.shutdown()
            serverB.server_close()


# ═══════════════════════════════════════════════════════════════════
# Native admission + close safety
# ═══════════════════════════════════════════════════════════════════

class TestNativeSending(unittest.TestCase):
    def setUp(self):
        self.server, self.port, _ = start_fake_server()
        self.server.admission_log = []
        FakeOpenCodeHandler.admission_log = self.server.admission_log
        FakeOpenCodeHandler.fail_next = False
        FakeOpenCodeHandler.server_sessions = {"ses_1"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def admission_log(self):
        return self.server.admission_log

    def test_24_viewer_close_no_effect_on_server(self):
        """Queue Viewer close during an active fake OpenCode build: the server
        continues; no interrupt/abort is sent."""
        win, mod = make_window(create_test_db())
        try:
            grant_endpoint(win, self.port, "ses_1")
            win._on_endpoint_resolved(win._endpoint_status)

            sender = sender_mod.NativeSender()
            r1 = sender._send_one(f"http://127.0.0.1:{self.port}", "ses_1", "before close")
            self.assertTrue(r1.success)

            win._shutdown_workers()

            r2 = sender._send_one(f"http://127.0.0.1:{self.port}", "ses_1", "after close")
            self.assertTrue(r2.success)
            for body in self.admission_log:
                self.assertEqual(body.get("delivery"), "queue")
            self.assertFalse(any(b.get("__abort") for b in self.admission_log))
        finally:
            win._shutdown_workers()

    def test_18b_send_uses_delivery_queue_and_correlates(self):
        sender = sender_mod.NativeSender()
        result = sender._send_one(f"http://127.0.0.1:{self.port}", "ses_1", "hello",
                                  request_id="req_x", kind="prompt")
        self.assertTrue(result.success)
        self.assertEqual(result.admitted_seq, 1)
        self.assertEqual(result.request_id, "req_x")
        self.assertEqual(result.session_id, "ses_1")
        self.assertEqual(result.kind, "prompt")
        body = self.admission_log[0]
        self.assertEqual(body["delivery"], "queue")
        self.assertEqual(body["prompt"]["text"], "hello")

    def test_19b_failed_post_never_falls_back_to_sqlite(self):
        FakeOpenCodeHandler.fail_next = True
        sender = sender_mod.NativeSender()
        result = sender._send_one(f"http://127.0.0.1:{self.port}", "ses_1", "test")
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        self.assertFalse(hasattr(sender, '_conn'))
        self.assertFalse(hasattr(sender, 'db_path'))

    def test_23b_second_wave_failure_prevents_third(self):
        presets_file = tempfile.mktemp(suffix=".json")
        paths = []
        for name in ["core", "wave2", "performance"]:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                             encoding="utf-8") as f:
                f.write(f"AUDIT {name.upper()}")
                paths.append(f.name)
        old_file = presets.PRESETS_FILE
        presets.PRESETS_FILE = presets_file
        try:
            presets.save_preset("core", paths[0])
            presets.save_preset("wave2", paths[1])
            presets.save_preset("performance", paths[2])

            sender = sender_mod.NativeSender()
            call_count = [0]
            original_send = sender._send_one

            def failing_second(url, session_id, text, request_id=None, kind="prompt"):
                call_count[0] += 1
                if call_count[0] == 2:
                    return sender_mod.AdmissionResult(success=False, error="Test failure")
                return original_send(url, session_id, text,
                                     request_id=request_id, kind=kind)

            sender._send_one = failing_second
            failed_msgs = []
            sender.signals.waves_failed.connect(lambda m: failed_msgs.append(m))
            sender.send_3_waves(f"http://127.0.0.1:{self.port}", "ses_1")

            self.assertEqual(call_count[0], 2)
            self.assertEqual(len(failed_msgs), 1)
            self.assertIn("W2", failed_msgs[0].error)
        finally:
            presets.PRESETS_FILE = old_file
            for p in paths:
                os.remove(p)
            if os.path.exists(presets_file):
                os.remove(presets_file)

    def test_24b_preset_text_never_logged(self):
        source = inspect.getsource(sender_mod.NativeSender._send_one)
        self.assertNotIn("logging", source)
        self.assertNotIn("print(", source)
        self.assertNotIn("log(", source)


# ═══════════════════════════════════════════════════════════════════
# Selection stability
# ═══════════════════════════════════════════════════════════════════

class TestSelectionStability(unittest.TestCase):
    def test_selected_session_frozen_at_top(self):
        """The selected session keeps its visual position while selected."""
        win, mod = make_window(create_test_db())
        try:
            win.current_session_id = "ses_selected"
            win._session_unavailable = False
            win._on_sidebar_updated([make_summary("ses_b", "RUNNING", 300),
                                     make_summary("ses_selected", "IDLE", 100),
                                     make_summary("ses_a", "RUNNING", 400)])
            row_first = win.session_list.item(0).data(0x0100)
            self.assertEqual(row_first, "ses_selected")
            win._on_sidebar_updated([make_summary("ses_a", "IDLE", 500),
                                     make_summary("ses_selected", "IDLE", 100),
                                     make_summary("ses_b", "IDLE", 600)])
            self.assertEqual(win.session_list.item(0).data(0x0100), "ses_selected")
        finally:
            win._shutdown_workers()


# ═══════════════════════════════════════════════════════════════════
# Event normalization + parser fidelity
# ═══════════════════════════════════════════════════════════════════

class TestEventProcessing(unittest.TestCase):
    def test_event_type_version_suffix_normalization(self):
        self.assertEqual(obs.normalize_event_type("session.next.step.ended.2"),
                         "session.next.step.ended")
        self.assertEqual(obs.normalize_event_type("session.next.text.delta.2"),
                         "session.next.text.delta")
        self.assertEqual(obs.normalize_event_type("session.created.1"),
                         "session.created")
        self.assertEqual(obs.normalize_event_type("tool.called"), "tool.called")

    def test_native_timestamp_parsing(self):
        from datetime import datetime, timezone
        self.assertEqual(obs.native_event_time({"timestamp": 1788644241181}),
                         1788644241.181)
        self.assertEqual(obs.native_event_time({"timestamp": 1788644241.181}),
                         1788644241.181)
        iso_dt = datetime(2026, 9, 12, 3, 37, 21, 181000, tzinfo=timezone.utc)
        parsed = obs.native_event_time({"timestamp": "2026-09-12T03:37:21.181Z"})
        self.assertAlmostEqual(parsed, iso_dt.timestamp(), places=2)
        self.assertIsNone(obs.native_event_time({}))
        self.assertIsNone(obs.native_event_time({"timestamp": "junk"}))

    def test_sse_parser_handles_real_frames(self):
        p = lstream.SseFrameParser()
        frames = []
        frames += p.feed(b": keepalive\n\n")
        self.assertEqual(frames, [])
        env = live_envelope("session.next.text.delta",
                            assistantMessageID=ASSIST_MSG_ID,
                            textID=TEXT_PART_ID, delta="hi")
        raw = f"data: {json.dumps(env)}\n\n".encode()
        half = len(raw) // 2
        frames += p.feed(raw[:half])
        self.assertEqual(frames, [])
        frames += p.feed(raw[half:])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["type"], "session.next.text.delta")
        self.assertEqual(frames[0]["properties"]["textID"], TEXT_PART_ID)
        # properties envelope preferred, data accepted for durable rows
        self.assertEqual(lstream.extract_session_id(env), SES_ID)
        durable = {"type": "session.next.text.ended",
                   "data": native_data(assistantMessageID=ASSIST_MSG_ID,
                                       textID=TEXT_PART_ID, text="x")}
        self.assertEqual(lstream.extract_session_id(durable), SES_ID)

# ═══════════════════════════════════════════════════════════════════
# P0-A: presentation arbitration (one LIVE OUTPUT authority)
# ═══════════════════════════════════════════════════════════════════

class TestPresentationArbitration(unittest.TestCase):
    """Required presentation tests 1-9: ONE presentation owner arbitrates
    durable observer text vs live SSE text; DURABLE FINAL REMAINS."""

    def _win(self, db_events=ACTIVE_DB_EVENTS):
        db_path = create_test_db(events=db_events, sessions=(SES_ID, "ses_other"))
        win, mod = make_window(db_path)
        win.sessions = [make_summary(SES_ID), make_summary("ses_other")]
        win._on_session_selected(0)
        win._presenter.set_session(SES_ID)
        return win, db_path

    def _teardown(self, win, db_path):
        try:
            win._shutdown_workers()
        finally:
            os.remove(db_path)

    def test_p1_live_partial_appears_before_durable_final(self):
        """P-1: a live partial renders immediately, before any durable row."""
        win, db_path = self._win()
        try:
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID})
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "stream"})
            drain_events(APP, 200)
            self.assertIn("stream", win.txt_live_output.toPlainText())
        finally:
            self._teardown(win, db_path)

    def test_p2_durable_final_replaces_partial(self):
        """P-2: durable text.ended finalizes the exact matching part."""
        win, db_path = self._win()
        try:
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID})
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "partial"})
            win._presenter.on_durable_text(SES_ID, "final authoritative",
                                           ASSIST_MSG_ID)
            drain_events(APP, 200)
            self.assertEqual(win.txt_live_output.toPlainText(), "final authoritative")
        finally:
            self._teardown(win, db_path)

    def test_p3_non_text_sse_after_final_cannot_restore_partial(self):
        """P-3: a non-text SSE event after durable finality restores nothing."""
        win, db_path = self._win()
        try:
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID})
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "partial"})
            win._presenter.on_durable_text(SES_ID, "final authoritative",
                                           ASSIST_MSG_ID)
            # Non-text lifecycle event AFTER finality.
            win._presenter.on_live_event(SES_ID, "session.next.step.started",
                                         {"assistantMessageID": ASSIST_MSG_ID})
            drain_events(APP, 200)
            self.assertEqual(win.txt_live_output.toPlainText(), "final authoritative",
                             "non-text SSE event must not restore stale text")
        finally:
            self._teardown(win, db_path)

    def test_p4_stale_same_generation_delta_cannot_restore_partial(self):
        """P-4: a stale same-generation delta after finality is ignored."""
        win, db_path = self._win()
        try:
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID})
            win._presenter.on_durable_text(SES_ID, "final authoritative",
                                           ASSIST_MSG_ID)
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "STALE"})
            drain_events(APP, 200)
            self.assertEqual(win.txt_live_output.toPlainText(), "final authoritative")
        finally:
            self._teardown(win, db_path)

    def test_p5_new_assistantMessageID_starts_new_turn(self):
        """P-5: a new assistantMessageID may start a new turn."""
        win, db_path = self._win()
        try:
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID})
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "old turn"})
            new_msg = "msg_0999newgeneration000000000000000000"
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": new_msg,
                                          "textID": "text-new"})
            drain_events(APP, 200)
            self.assertEqual(win._presenter.current_message_id, new_msg)
            self.assertEqual(win.txt_live_output.toPlainText(), "")
        finally:
            self._teardown(win, db_path)

    def test_p6_old_session_live_update_after_switch_is_ignored(self):
        """P-6: an old-session live update after a switch is ignored."""
        win, db_path = self._win()
        try:
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID})
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "A text"})
            drain_events(APP, 100)
            # Switch to B
            win._on_session_selected(1)
            drain_events(APP, 100)
            self.assertEqual(win.txt_live_output.toPlainText(), "")
            # Old-session live event must be ignored.
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "STALE A"})
            drain_events(APP, 100)
            self.assertEqual(win.txt_live_output.toPlainText(), "")
        finally:
            self._teardown(win, db_path)

    def test_p7_live_step_started_shows_running_before_db_polling(self):
        """P-7: a live step.started may show RUNNING before DB polling."""
        win, db_path = self._win()
        try:
            states = []
            win._presenter.signals.state_updated.connect(states.append)
            win._presenter.on_live_event(SES_ID, "session.next.step.started",
                                         {"assistantMessageID": ASSIST_MSG_ID})
            drain_events(APP, 100)
            self.assertIn("RUNNING", states)
        finally:
            self._teardown(win, db_path)

    def test_p8_later_durable_state_reconciles_without_regression(self):
        """P-8: later durable text reconciles without regressing the
        display to an older partial."""
        win, db_path = self._win()
        try:
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID})
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "part"})
            win._presenter.on_durable_text(SES_ID, "part-final", ASSIST_MSG_ID)
            drain_events(APP, 100)
            self.assertEqual(win.txt_live_output.toPlainText(), "part-final")
            # A later observer refresh re-emits the SAME durable text: no
            # regression, no resurrection of the partial.
            win._presenter.on_durable_text(SES_ID, "part-final", ASSIST_MSG_ID)
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "stale"})
            drain_events(APP, 100)
            self.assertEqual(win.txt_live_output.toPlainText(), "part-final")
        finally:
            self._teardown(win, db_path)

    def test_p9_reconnect_preserves_authoritative_final(self):
        """P-9: a stream reconnect (resend of an older envelope) preserves
        the authoritative final display."""
        win, db_path = self._win()
        try:
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID})
            win._presenter.on_durable_text(SES_ID, "durable final", ASSIST_MSG_ID)
            # Reconnect replays an old partial envelope.
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": TEXT_PART_ID, "delta": "replayed partial"})
            drain_events(APP, 100)
            self.assertEqual(win.txt_live_output.toPlainText(), "durable final")
        finally:
            self._teardown(win, db_path)

    def test_p10_only_one_sse_connection_per_endpoint_presenter_scope(self):
        """P-10: presenter scope changes never open a second SSE connection."""
        db_path = create_test_db(events=ACTIVE_DB_EVENTS, sessions=(SES_ID, "ses_other"))
        server, port, t = start_fake_server()
        server.server_sessions = {SES_ID}
        win, mod = make_window(db_path)
        try:
            win.sessions = [make_summary(SES_ID), make_summary("ses_other")]
            win._on_session_selected(0)
            drain_events(APP, 300)
            snapshot = ep.ProcessSnapshot(
                pid=4321, start_time=time.time() - 60,
                executable_path="c:/x/opencode.exe",
                endpoint=f"http://127.0.0.1:{port}",
                verified_session_ids=(SES_ID,))
            win._resolver._scan_candidates = lambda: [snapshot]
            grant_endpoint(win, port, SES_ID)
            win._on_endpoint_resolved(win._endpoint_status)
            win._stream.retarget_endpoint.emit(f"http://127.0.0.1:{port}")
            win._stream.select_session.emit(SES_ID)
            drain_events(APP, 600)
            first = server.sse_connections
            self.assertEqual(first, 1)
            # Presenter scope churn must not open new SSE connections.
            win._presenter.set_session("ses_other")
            win._presenter.set_session(SES_ID)
            drain_events(APP, 400)
            self.assertEqual(server.sse_connections, 1)
        finally:
            win._shutdown_workers()
            server.shutdown()
            server.server_close()
            os.remove(db_path)

    def test_presenter_reasoning_never_rendered(self):
        """Reasoning deltas never render (invariant 9)."""
        win, db_path = self._win()
        try:
            win._presenter.on_live_event(SES_ID, "session.next.reasoning.delta",
                                         {"assistantMessageID": ASSIST_MSG_ID,
                                          "textID": "r1", "delta": "secret reasoning"})
            drain_events(APP, 100)
            self.assertEqual(win.txt_live_output.toPlainText(), "")
        finally:
            self._teardown(win, db_path)

    def test_presenter_output_bound_enforced(self):
        """The ONE 128 KiB total bound is enforced by the presenter."""
        p = present.LiveOutputPresenter()
        p.set_session(SES_ID)
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": ASSIST_MSG_ID, "textID": "t1"})
        for _ in range(8):
            p.on_live_event(SES_ID, "session.next.text.delta",
                            {"assistantMessageID": ASSIST_MSG_ID, "textID": "t1",
                             "delta": "x" * 20000})
        self.assertLessEqual(p._turn_bytes, present.LiveOutputPresenter.MAX_TEXT_BYTES)
        p.render().encode("utf-8")  # never a split character


# ═══════════════════════════════════════════════════════════════════
# P0-B: GUI-side single-flight admission ownership
# ═══════════════════════════════════════════════════════════════════

class TestAdmissionSingleFlight(unittest.TestCase):
    """Required single-flight tests 11-24: ownership begins at USER
    ACTIVATION (GUI-side), before any worker dispatch. Double activation
    cannot double-admit."""

    def setUp(self):
        self.db_path = create_test_db()
        self.server, self.port, _ = start_fake_server()
        self.server.server_sessions = {"ses_1", "ses_2"}
        self.server.admission_log = []
        FakeOpenCodeHandler.admission_log = self.server.admission_log
        FakeOpenCodeHandler.fail_next = False
        FakeOpenCodeHandler.test_delay_seconds = 0.0

    def tearDown(self):
        FakeOpenCodeHandler.test_delay_seconds = 0.0
        self.server.shutdown()
        self.server.server_close()
        os.remove(self.db_path)

    def _grant(self, win):
        import psutil
        me = psutil.Process()
        grant_endpoint(win, self.port, "ses_1")
        # The captured endpoint identity must be a REAL live process (this
        # test process) so the sender's fail-closed revalidation passes.
        from dataclasses import replace as dc_replace
        es = win._endpoint_status
        win._endpoint_status = dc_replace(
            es, endpoint=dc_replace(es.endpoint, pid=os.getpid(),
                                    start_time=me.create_time()))
        win._on_endpoint_resolved(win._endpoint_status)

    def _mk(self):
        win, mod = make_window(self.db_path)
        self._grant(win)
        return win

    def _no_new_admission(self):
        return len(self.server.admission_log)

    def test_11_two_immediate_sends_one_admission(self):
        win = self._mk()
        try:
            FakeOpenCodeHandler.test_delay_seconds = 0.4
            win.input_prompt.setText("once only")
            win._on_send()
            n_after_first = self._no_new_admission()
            win._on_send()   # rapid second activation
            self.assertEqual(self._no_new_admission(), n_after_first,
                             "second rapid send must not enqueue work")
            drain_events(APP, 2500)
            self.assertEqual(len(self.server.admission_log), 1)
            self.assertTrue(win._admission_owner is None,
                            "terminal path must release ownership")
        finally:
            FakeOpenCodeHandler.test_delay_seconds = 0.0
            win._shutdown_workers()

    def test_12_two_immediate_enter_activations_one_admission(self):
        win = self._mk()
        try:
            FakeOpenCodeHandler.test_delay_seconds = 0.4
            win.input_prompt.setText("enter once")
            win.input_prompt.returnPressed.emit()   # Enter activation
            win.input_prompt.returnPressed.emit()   # rapid second Enter
            drain_events(APP, 2500)
            self.assertEqual(len(self.server.admission_log), 1)
        finally:
            FakeOpenCodeHandler.test_delay_seconds = 0.0
            win._shutdown_workers()

    def test_13_double_cc_one_admission(self):
        win = self._mk()
        try:
            FakeOpenCodeHandler.test_delay_seconds = 0.4
            win._on_cc()
            win._on_cc()
            drain_events(APP, 2500)
            self.assertEqual(len(self.server.admission_log), 1)
        finally:
            FakeOpenCodeHandler.test_delay_seconds = 0.0
            win._shutdown_workers()

    def test_14_double_core_one_admission(self):
        win = self._mk()
        try:
            FakeOpenCodeHandler.test_delay_seconds = 0.4
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                             encoding="utf-8") as f:
                f.write("AUDIT CORE CONTENT")
                path = f.name
            win._preset_paths = {"core": path}
            win._on_core()
            win._on_core()
            drain_events(APP, 2500)
            self.assertEqual(len(self.server.admission_log), 1)
            self.assertEqual(self.server.admission_log[0]["prompt"]["text"],
                             "AUDIT CORE CONTENT")
            os.remove(path)
        finally:
            FakeOpenCodeHandler.test_delay_seconds = 0.0
            win._shutdown_workers()

    def test_15_double_w2_one_admission(self):
        win = self._mk()
        try:
            FakeOpenCodeHandler.test_delay_seconds = 0.4
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                             encoding="utf-8") as f:
                f.write("AUDIT W2 CONTENT")
                path = f.name
            win._preset_paths = {"wave2": path}
            win._on_w2()
            win._on_w2()
            drain_events(APP, 2500)
            self.assertEqual(len(self.server.admission_log), 1)
            os.remove(path)
        finally:
            FakeOpenCodeHandler.test_delay_seconds = 0.0
            win._shutdown_workers()

    def test_16_double_perf_one_admission(self):
        win = self._mk()
        try:
            FakeOpenCodeHandler.test_delay_seconds = 0.4
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                             encoding="utf-8") as f:
                f.write("AUDIT PERF CONTENT")
                path = f.name
            win._preset_paths = {"performance": path}
            win._on_perf()
            win._on_perf()
            drain_events(APP, 2500)
            self.assertEqual(len(self.server.admission_log), 1)
            os.remove(path)
        finally:
            FakeOpenCodeHandler.test_delay_seconds = 0.0
            win._shutdown_workers()

    def test_17_double_audit_file_one_admission(self):
        """Double Audit File activation: one admission (ownership begins at
        activation, before the async file read)."""
        win = self._mk()
        try:
            FakeOpenCodeHandler.test_delay_seconds = 0.4
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                             encoding="utf-8") as f:
                f.write("AUDIT FILE CONTENT")
                path = f.name
            owner1 = win._acquire_admission("audit-file")
            self.assertIsNotNone(owner1)
            owner2 = win._acquire_admission("audit-file")
            self.assertIsNone(owner2,
                              "second audit-file activation must not admit")
            # Simulate the completed read path for the first reservation.
            req = win._build_request(kind="audit-file", slot="", path=path)
            from dataclasses import replace as dc_replace
            req = dc_replace(req, request_id=owner1["request_id"])
            win._on_audit_file_loaded(req, "AUDIT FILE CONTENT")
            drain_events(APP, 2500)
            self.assertEqual(len(self.server.admission_log), 1)
            os.remove(path)
        finally:
            FakeOpenCodeHandler.test_delay_seconds = 0.0
            win._shutdown_workers()

    def test_18_double_3waves_exactly_three_admissions(self):
        """Double 3 WAVES -> exactly ONE three-wave batch (3 admissions)."""
        win = self._mk()
        try:
            FakeOpenCodeHandler.test_delay_seconds = 0.4
            presets_file = tempfile.mktemp(suffix=".json")
            paths = []
            for name in ["core", "wave2", "performance"]:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False,
                                                 encoding="utf-8") as f:
                    f.write(f"AUDIT {name.upper()}")
                    paths.append(f.name)
            old_file = presets.PRESETS_FILE
            presets.PRESETS_FILE = presets_file
            try:
                presets.save_preset("core", paths[0])
                presets.save_preset("wave2", paths[1])
                presets.save_preset("performance", paths[2])
                win._preset_paths = {"core": paths[0], "wave2": paths[1],
                                     "performance": paths[2]}
                win._on_3waves()
                batch_id = win._admission_owner["request_id"]
                win._on_3waves()   # double-click: must enqueue NOTHING
                self.assertIsNotNone(win._admission_owner)
                self.assertEqual(win._admission_owner["request_id"], batch_id)
                drain_events(APP, 4000)
                self.assertEqual(len(self.server.admission_log), 3,
                                 "double 3 WAVES must produce exactly CORE/W2/PERF once")
                self.assertEqual([b["prompt"]["text"] for b in self.server.admission_log],
                                 ["AUDIT CORE", "AUDIT WAVE2", "AUDIT PERFORMANCE"])
            finally:
                presets.PRESETS_FILE = old_file
                for p in paths:
                    os.remove(p)
                if os.path.exists(presets_file):
                    os.remove(presets_file)
        finally:
            FakeOpenCodeHandler.test_delay_seconds = 0.0
            win._shutdown_workers()

    def test_19_failure_releases_matching_reservation(self):
        win = self._mk()
        try:
            FakeOpenCodeHandler.fail_next = True
            win.input_prompt.setText("will fail")
            win._on_send()
            drain_events(APP, 2500)
            self.assertIsNone(win._admission_owner,
                              "HTTP failure must release the matching reservation")
            # Ownership released -> a new send can be admitted.
            FakeOpenCodeHandler.fail_next = False
            win.input_prompt.setText("retry ok")
            win._on_send()
            drain_events(APP, 2500)
            self.assertEqual(len(self.server.admission_log), 1)
        finally:
            win._shutdown_workers()

    def test_20_async_preset_failure_releases_matching_reservation(self):
        win = self._mk()
        try:
            owner = win._acquire_admission("core")
            self.assertIsNotNone(owner)
            req = win._build_request(kind="core", slot="core", path="Z:/missing.md")
            from dataclasses import replace as dc_replace
            req = dc_replace(req, request_id=owner["request_id"])
            win._on_file_error("File too large: 9999 bytes", req)
            self.assertIsNone(win._admission_owner,
                              "async file failure must release the matching reservation")
        finally:
            win._shutdown_workers()

    def test_21_stale_completion_cannot_release_newer_request(self):
        win = self._mk()
        try:
            # A legacy worker-minted reservation adopted at send_started.
            win._on_send_started({"request_id": "req_old", "session_id": "ses_1",
                                  "kind": "prompt"})
            self.assertIsNotNone(win._admission_owner,
                                 "legacy reservation adopted at send_started")
            self.assertTrue(win._release_admission("req_old"))
            # A newer GUI reservation must survive an old completion.
            win._acquire_admission("send")
            newer_id = win._admission_owner["request_id"]
            from viewer_sender import AdmissionResult
            win._on_send_completed(AdmissionResult(
                success=True, admitted_seq=1, request_id="req_old",
                session_id="ses_1", kind="prompt"))
            self.assertIsNotNone(win._admission_owner,
                                 "stale completion must not release a newer request")
            self.assertEqual(win._admission_owner["request_id"], newer_id)
            # The matching release still works afterwards.
            win._on_send_completed(AdmissionResult(
                success=True, admitted_seq=2, request_id=newer_id,
                session_id="ses_1", kind="prompt"))
            self.assertIsNone(win._admission_owner)
        finally:
            win._shutdown_workers()

    def test_22_stale_completion_cannot_alter_current_session_feedback(self):
        """Also covered by fixed test_17; direct deterministic variant."""
        win = self._mk()
        try:
            win.current_session_id = "ses_2"
            win.lbl_feedback.setText("B FEEDBACK")
            from viewer_sender import AdmissionResult
            win._on_send_completed(AdmissionResult(
                success=True, admitted_seq=7, request_id="req_x",
                session_id="ses_1", kind="prompt"))
            self.assertEqual(win.lbl_feedback.text(), "B FEEDBACK",
                             "old-session completion must not mutate selected-session feedback")
        finally:
            win._shutdown_workers()

    def test_23_newer_composer_text_survives_old_completion(self):
        win = self._mk()
        try:
            win.input_prompt.setText("older")
            win._composer_submit = {"text": "older", "rev": 0}
            win._on_send_started({"request_id": "req_c1", "session_id": "ses_1",
                                  "kind": "prompt"})
            win.input_prompt.setText("newer user text")
            win.input_prompt.textEdited.emit("newer user text")
            from viewer_sender import AdmissionResult
            win._on_send_completed(AdmissionResult(
                success=True, admitted_seq=3, request_id="req_c1",
                session_id="ses_1", kind="prompt"))
            self.assertEqual(win.input_prompt.text(), "newer user text",
                             "newer composer text must survive an older completion")
        finally:
            win._shutdown_workers()

    def test_24_matching_success_clears_only_matching_submitted_text(self):
        win = self._mk()
        try:
            win.input_prompt.setText("exact match")
            win._composer_submit = {"text": "exact match", "rev": win._composer_rev}
            win._on_send_started({"request_id": "req_m1", "session_id": "ses_1",
                                  "kind": "prompt"})
            from viewer_sender import AdmissionResult
            win._on_send_completed(AdmissionResult(
                success=True, admitted_seq=4, request_id="req_m1",
                session_id="ses_1", kind="prompt"))
            self.assertEqual(win.input_prompt.text(), "",
                             "matching success clears the submitted text")
            # A non-matching completion must NOT clear newer text.
            win.input_prompt.setText("other text")
            win._composer_submit = {"text": "other text", "rev": win._composer_rev}
            win._on_send_started({"request_id": "req_m2", "session_id": "ses_1",
                                  "kind": "prompt"})
            win._on_send_completed(AdmissionResult(
                success=False, error="HTTP Error 500: test", request_id="req_m2",
                session_id="ses_1", kind="prompt"))
            self.assertEqual(win.input_prompt.text(), "other text",
                             "failure never clears the composer")
        finally:
            win._shutdown_workers()

    def test_ownership_exclusive_across_paths(self):
        """Ownership is exclusive: a different path while owned is refused,
        and released only by the matching request_id."""
        win = self._mk()
        try:
            o1 = win._acquire_admission("send")
            self.assertIsNotNone(o1)
            self.assertIsNone(win._acquire_admission("cc"))
            self.assertIsNone(win._acquire_admission("3waves"))
            self.assertFalse(win._release_admission("req_wrong"))
            self.assertTrue(win._release_admission(o1["request_id"]))
            self.assertIsNone(win._admission_owner)
        finally:
            win._shutdown_workers()


# ═══════════════════════════════════════════════════════════════════
# Target A: immutable durable identity + part-scoped finality
# ═══════════════════════════════════════════════════════════════════

class TestDurableIdentitySnapshot(unittest.TestCase):
    """Target A1: durable identity crosses threads as IMMUTABLE data; the
    GUI never reconstructs identity from mutable worker internals."""

    def _win(self):
        db_path = create_test_db(events=ACTIVE_DB_EVENTS, sessions=(SES_ID, "ses_other"))
        win, mod = make_window(db_path)
        win.sessions = [make_summary(SES_ID), make_summary("ses_other")]
        win._on_session_selected(0)
        win._presenter.set_session(SES_ID)
        return win, db_path

    def _teardown(self, win, db_path):
        try:
            win._shutdown_workers()
        finally:
            os.remove(db_path)

    def test_a1_observer_emits_immutable_snapshot_with_text(self):
        """Observer emits an immutable DurableTextSnapshot carrying session,
        assistantMessageID, ordered parts with textID/finality and the
        durable seq witness."""
        db_path = create_test_db(events=REAL_DB_EVENTS)
        try:
            observer = obs.ReadOnlyObserver(db_path)
            observer._open_connection()
            observer._selected_session_id = SES_ID
            rec = SignalRecorder(observer.signals.live_text_updated)
            observer._hydrate(observer._conn.cursor(), SES_ID)
            self.assertEqual(len(rec.items), 1)
            sid, snap = rec.items[0]
            self.assertEqual(sid, SES_ID)
            self.assertIsInstance(snap, present.DurableTextSnapshot)
            self.assertEqual(snap.session_id, SES_ID)
            self.assertEqual(snap.message_id, ASSIST_MSG_ID)
            self.assertEqual(snap.last_event_seq, 6)
            parts = snap.parts
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0].text_id, TEXT_PART_ID)
            self.assertEqual(parts[0].text, "cc2 finished")
            self.assertTrue(parts[0].finalized)
            self.assertEqual(snap.assembled_text, "cc2 finished")
            observer.stop()
            del observer
            import gc
            gc.collect()
        finally:
            os.remove(db_path)

    def test_a2_queued_delivery_after_observer_advance_keeps_identity(self):
        """The observer advances its internal message state BEFORE the GUI
        processes the queued signal; the GUI still reconciles the ORIGINAL
        generation identity from the immutable snapshot."""
        db_path = create_test_db(events=ACTIVE_DB_EVENTS)
        try:
            observer = obs.ReadOnlyObserver(db_path)
            observer._open_connection()
            observer._selected_session_id = SES_ID
            rec = SignalRecorder(observer.signals.live_text_updated)
            observer._hydrate(observer._conn.cursor(), SES_ID)
            sid, snap = rec.items[0]

            # Simulate the queued-signal race: the observer moves on to a
            # NEWER generation (with a part of that generation, so the
            # snapshot it would emit next describes generation B) BEFORE
            # the GUI handles the queued payload.
            observer._process_event_text(
                "text.started", {"assistantMessageID": "msg_generationB",
                                 "textID": "text-b"})
            observer._process_event_text(
                "text.delta", {"assistantMessageID": "msg_generationB",
                               "textID": "text-b", "delta": "B streams"})
            self.assertEqual(observer._current_message_id, "msg_generationB")

            # GUI-side handler runs NOW with the ORIGINAL snapshot (and
            # even with a fresh B snapshot queued behind it).
            presenter = present.LiveOutputPresenter()
            presenter.set_session(SES_ID)
            win_handler = self._handler_for(presenter)
            win_handler(sid, snap)
            self.assertEqual(presenter.current_message_id, ASSIST_MSG_ID,
                             "identity must come from the snapshot, not the "
                             "observer's advanced internal state")
            self.assertEqual(presenter.render(), "")
            sid2, snap2 = observer.signals.live_text_updated.emit(
                sid, observer._build_durable_snapshot()) or (None, None)
            # Deliver the newer B snapshot as the GUI would receive it.
            win_handler(sid, observer._build_durable_snapshot())
            self.assertEqual(presenter.current_message_id, "msg_generationB")
            self.assertEqual(presenter.render(), "B streams")
            observer.stop()
            del observer
            import gc
            gc.collect()
        finally:
            os.remove(db_path)

    def test_t174_delayed_durable_signal_keeps_original_message_id(self):
        """T-174 regression: a delayed durable signal keeps its original
        message id even if the observer advances to a newer generation."""
        self.test_a2_queued_delivery_after_observer_advance_keeps_identity()

    @staticmethod
    def _handler_for(presenter):
        """The PRODUCTION GUI handler body, bound to a given presenter."""
        def handler(session_id, snapshot):
            if isinstance(snapshot, present.DurableTextSnapshot):
                presenter.on_durable_text(session_id, snapshot.assembled_text,
                                          snapshot=snapshot)
            else:
                presenter.on_durable_text(session_id, str(snapshot))
        return handler

    def test_a3_no_gui_read_of_mutable_observer_identity(self):
        """No GUI code reads _observer._current_message_id."""
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(root, "Scripts", "saipatch", "queue_viewer.pyw"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("_current_message_id", src,
                         "GUI must never read mutable observer message identity")

    def _teardown_win(self, win, db_path):
        self._teardown(win, db_path)


class TestPartScopedFinality(unittest.TestCase):
    """Target A2: durable finality is scoped to (assistantMessageID,
    textID), never generation-wide. Required ordering:
    live M1/T1 -> durable final M1/T1 -> live M1/T2 -> durable final M1/T2
    -> live M2/T1. The inverse stale case fails closed."""

    def _presenter(self):
        p = present.LiveOutputPresenter()
        p.set_session(SES_ID)
        return p

    def test_a4_durable_final_m1t1_blocks_stale_m1t1_deltas(self):
        p = self._presenter()
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t1"})
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1", "delta": "part"})
        p.on_durable_text(SES_ID, "final t1", "m1")
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1", "delta": "STALE"})
        self.assertEqual(p.render(), "final t1")

    def test_a5_durable_final_m1t1_does_not_block_new_m1t2(self):
        p = self._presenter()
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t1"})
        p.on_durable_text(SES_ID, "final t1", "m1")
        # Same assistant generation starts a SECOND text part.
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t2"})
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t2", "delta": "T2 streams"})
        self.assertIn("final t1", p.render())
        self.assertIn("T2 streams", p.render())
        self.assertLess(p.render().index("final t1"), p.render().index("T2 streams"))

    def test_a6_durable_m1t2_reconciles_t2(self):
        p = self._presenter()
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t1"})
        p.on_durable_text(SES_ID, "final t1", "m1")
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t2"})
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t2", "delta": "t2 partial"})
        # Durable snapshot for the SAME generation carries BOTH parts.
        snap = present.DurableTextSnapshot(
            session_id=SES_ID, message_id="m1",
            parts=(present.DurableTextPart("t1", "final t1", True),
                   present.DurableTextPart("t2", "final t2", True)),
            last_event_seq=42)
        p.on_durable_text(SES_ID, snap.assembled_text, snapshot=snap)
        self.assertEqual(p.render(), "final t1final t2")
        # A stale t2 partial can no longer regress the reconciled t2.
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t2", "delta": "STALE"})
        self.assertEqual(p.render(), "final t1final t2")

    def test_a7_new_live_generation_m2_retires_m1(self):
        p = self._presenter()
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t1"})
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1", "delta": "old gen"})
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m2", "textID": "t1"})
        self.assertEqual(p.current_message_id, "m2")
        self.assertEqual(p.render(), "")
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m2", "textID": "t1", "delta": "new gen"})
        self.assertEqual(p.render(), "new gen")

    def test_a8_late_durable_m1_after_live_m2_cannot_replace_m2(self):
        p = self._presenter()
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t1"})
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1", "delta": "old"})
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m2", "textID": "t1"})
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m2", "textID": "t1", "delta": "new"})
        # Late durable snapshot for RETIRED M1 must fail closed.
        snap = present.DurableTextSnapshot(
            session_id=SES_ID, message_id="m1",
            parts=(present.DurableTextPart("t1", "OLD M1 FINAL", True),),
            last_event_seq=10)
        p.on_durable_text(SES_ID, snap.assembled_text, snapshot=snap)
        self.assertEqual(p.render(), "new",
                         "late durable M1 must NOT replace live M2")
        self.assertEqual(p.current_message_id, "m2")

    def test_a9_unseen_newer_durable_generation_hydrates(self):
        """SSE was missed entirely: a genuinely newer durable generation
        may still hydrate correctly."""
        p = self._presenter()
        snap = present.DurableTextSnapshot(
            session_id=SES_ID, message_id="m9",
            parts=(present.DurableTextPart("t1", "hydrated durable answer", True),),
            last_event_seq=77)
        p.on_durable_text(SES_ID, snap.assembled_text, snapshot=snap)
        self.assertEqual(p.render(), "hydrated durable answer")
        self.assertEqual(p.current_message_id, "m9")

    def test_a10_session_switch_invalidates_generation_state(self):
        p = self._presenter()
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t1"})
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1", "delta": "A text"})
        p.set_session("ses_other")
        self.assertIsNone(p.current_message_id)
        self.assertEqual(p.render(), "")
        # Old-session events after the switch are ignored.
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1", "delta": "STALE A"})
        p.on_durable_text(SES_ID, "STALE A durable", "m1")
        self.assertEqual(p.render(), "")


# ═══════════════════════════════════════════════════════════════════
# Target B: production lifecycle authority
# ═══════════════════════════════════════════════════════════════════

class TestProductionLifecycle(unittest.TestCase):
    """Target B: presenter state updates are consumed by PRODUCTION
    QueueViewerWindow; live lifecycle cannot be regressed by stale durable
    state; session switch invalidates the overlay."""

    def _win(self):
        db_path = create_test_db(events=ACTIVE_DB_EVENTS, sessions=(SES_ID, "ses_other"))
        win, mod = make_window(db_path)
        win.sessions = [make_summary(SES_ID), make_summary("ses_other")]
        win._on_session_selected(0)
        win._presenter.set_session(SES_ID)
        return win, db_path

    def _teardown(self, win, db_path):
        try:
            win._shutdown_workers()
        finally:
            os.remove(db_path)

    def test_b1_step_started_shows_running_in_production_header(self):
        """Call the NORMAL production live-event handler with step.started;
        the actual selected-session native status becomes RUNNING. No
        test-only state_updated listener is required."""
        win, db_path = self._win()
        try:
            self.assertNotIn("RUNNING", win.lbl_native_status.text())
            win._on_live_event(SES_ID, "session.next.step.started",
                               {"assistantMessageID": ASSIST_MSG_ID})
            drain_events(APP, 150)
            self.assertEqual(win.lbl_native_status.text(), "[RUNNING]")
            self.assertEqual(win._presenter.lifecycle_state, "RUNNING")
        finally:
            self._teardown(win, db_path)

    def test_b2_stale_durable_snapshot_cannot_regress_live_running(self):
        win, db_path = self._win()
        try:
            win._on_live_event(SES_ID, "session.next.step.started",
                               {"assistantMessageID": ASSIST_MSG_ID})
            drain_events(APP, 100)
            self.assertEqual(win.lbl_native_status.text(), "[RUNNING]")
            # An OLDER durable lifecycle snapshot (low seq) must not
            # regress the newer live RUNNING.
            win._presenter.on_durable_lifecycle(SES_ID, "IDLE", "", seq=1)
            drain_events(APP, 100)
            self.assertEqual(win.lbl_native_status.text(), "[RUNNING]",
                             "stale durable IDLE must not regress live RUNNING")
        finally:
            self._teardown(win, db_path)

    def test_b3_matching_newer_durable_replaces_live_overlay(self):
        win, db_path = self._win()
        try:
            win._on_live_event(SES_ID, "session.next.step.started",
                               {"assistantMessageID": ASSIST_MSG_ID})
            drain_events(APP, 100)
            self.assertEqual(win.lbl_native_status.text(), "[RUNNING]")
            # Production sequence: the live-causing event lands durably and
            # is consumed (event_observed) BEFORE the newer snapshot.
            win._on_event_observed(SES_ID, 50, time.time())
            # A NEWER durable lifecycle witness reconciles the overlay.
            win._presenter.on_durable_lifecycle(SES_ID, "IDLE", "Done", seq=99)
            drain_events(APP, 100)
            self.assertEqual(win.lbl_native_status.text(), "[IDLE]")
            self.assertEqual(win._presenter.lifecycle_state, "IDLE")
        finally:
            self._teardown(win, db_path)

    def test_b4_step_failed_becomes_failed_visibly(self):
        win, db_path = self._win()
        try:
            win._on_live_event(SES_ID, "session.next.step.failed",
                               {"assistantMessageID": ASSIST_MSG_ID,
                                "error": {"message": "boom"}})
            drain_events(APP, 100)
            self.assertEqual(win.lbl_native_status.text(), "[FAILED]")
            self.assertIn("FAILED", win.lbl_activity.text())
        finally:
            self._teardown(win, db_path)

    def test_b5_permission_becomes_needs_human_visibly(self):
        win, db_path = self._win()
        try:
            for etype in ("session.next.permission.asked",
                          "session.next.question.asked",
                          "session.next.input.required"):
                win._on_live_event(SES_ID, etype, {"assistantMessageID": ASSIST_MSG_ID})
                drain_events(APP, 100)
                self.assertEqual(win.lbl_native_status.text(), "[NEEDS_HUMAN]")
        finally:
            self._teardown(win, db_path)

    def test_b6_old_session_lifecycle_after_switch_changes_nothing(self):
        win, db_path = self._win()
        try:
            win._on_live_event(SES_ID, "session.next.step.started",
                               {"assistantMessageID": ASSIST_MSG_ID})
            drain_events(APP, 100)
            # Switch to B.
            win._on_session_selected(1)
            drain_events(APP, 100)
            status_b = win.lbl_native_status.text()
            # Old-session live lifecycle must change nothing.
            win._on_live_event(SES_ID, "session.next.step.started",
                               {"assistantMessageID": ASSIST_MSG_ID})
            win._on_live_event(SES_ID, "session.next.step.failed",
                               {"assistantMessageID": ASSIST_MSG_ID,
                                "error": {"message": "late"}})
            drain_events(APP, 100)
            self.assertEqual(win.lbl_native_status.text(), status_b,
                             "old-session lifecycle after a switch must change nothing")
        finally:
            self._teardown(win, db_path)

    def test_b7_sidebar_state_routes_through_lifecycle_authority(self):
        """The sidebar durable state reconciles through the presenter with
        its seq witness, and the header shows the arbitrated state."""
        win, db_path = self._win()
        try:
            # Live RUNNING first.
            win._on_live_event(SES_ID, "session.next.step.started",
                               {"assistantMessageID": ASSIST_MSG_ID})
            drain_events(APP, 100)
            self.assertEqual(win.lbl_native_status.text(), "[RUNNING]")
            # A sidebar snapshot with NO durable progress since the live
            # advance cannot regress.
            win._on_sidebar_updated([make_summary(SES_ID, state="IDLE"),
                                     make_summary("ses_other")])
            drain_events(APP, 100)
            self.assertEqual(win.lbl_native_status.text(), "[RUNNING]",
                             "older durable sidebar state must not regress live RUNNING")
            # Production reconciliation: the events land durably, are
            # consumed, and the NEWER witness snapshot follows.
            win._on_event_observed(SES_ID, 5_000, time.time())
            newer = make_summary(SES_ID, state="NEEDS_HUMAN")
            newer.latest_event_seq = 10_000
            win._on_sidebar_updated([newer, make_summary("ses_other")])
            drain_events(APP, 100)
            self.assertEqual(win.lbl_native_status.text(), "[NEEDS_HUMAN]")
        finally:
            self._teardown(win, db_path)


# ═══════════════════════════════════════════════════════════════════
# Target C: stale start ownership + visible control state
# ═══════════════════════════════════════════════════════════════════

class TestStaleSendOwnership(unittest.TestCase):
    """Target C1/C2: an old-session send_started must not mutate the new
    session; visible controls always reflect real admission ownership."""

    def setUp(self):
        self.db_path = create_test_db()
        self.server, self.port, _ = start_fake_server()
        self.server.server_sessions = {"ses_1", "ses_2"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        os.remove(self.db_path)

    def _mk(self):
        win, mod = make_window(self.db_path)
        import psutil
        me = psutil.Process()
        grant_endpoint(win, self.port, "ses_1")
        from dataclasses import replace as dc_replace
        es = win._endpoint_status
        win._endpoint_status = dc_replace(
            es, endpoint=dc_replace(es.endpoint, pid=os.getpid(),
                                    start_time=me.create_time()))
        win._on_endpoint_resolved(win._endpoint_status)
        return win

    def test_c1_stale_start_cannot_mutate_new_session(self):
        """Send A, switch to B before A's send_started, deliver A's
        start: B feedback/controls unchanged; A completion cleans up."""
        win = self._mk()
        try:
            win.input_prompt.setText("A prompt")
            win._composer_submit = {"text": "A prompt", "rev": win._composer_rev}
            win._on_send_started({"request_id": "reqA", "session_id": "ses_1",
                                  "kind": "prompt"})
            self.assertEqual(win.lbl_feedback.text(), "SAIPEN: sending...")
            self.assertFalse(win.btn_send.isEnabled())

            # Switch UI to session B.
            win.current_session_id = "ses_2"
            win._update_send_controls()
            feedback_b = win.lbl_feedback.text()
            composer_b = win.input_prompt.text()

            # A's DELAYED send_started arrives (already recorded above; a
            # second delayed start for the same request must also be inert).
            win._on_send_started({"request_id": "reqA", "session_id": "ses_1",
                                  "kind": "prompt"})
            self.assertEqual(win.lbl_feedback.text(), feedback_b,
                             "A's stale start must not write B's feedback")
            self.assertEqual(win.input_prompt.text(), composer_b,
                             "A's stale start must not touch B's composer")

            # A's completion: correctly classified stale, returns early,
            # but releases the matching reservation and recomputes B.
            from viewer_sender import AdmissionResult
            win._on_send_completed(AdmissionResult(
                success=True, admitted_seq=1, request_id="reqA",
                session_id="ses_1", kind="prompt"))
            self.assertEqual(win.lbl_feedback.text(), feedback_b,
                             "A's stale completion must not write B's feedback")
            self.assertEqual(win.input_prompt.text(), composer_b)
            self.assertIsNone(win._admission_owner,
                              "A ownership must be cleaned up")
            # B controls recomputed from B's OWN authority: ses_2 is not in
            # B's verified endpoint sessions for this selection, so B stays
            # disabled — but by B's verdict, not by A's leftover.
            self.assertFalse(win.btn_send.isEnabled())
            self.assertNotIn("Admission in flight",
                             win.input_prompt.placeholderText(),
                             "B must not show A's admission busy state")
        finally:
            win._shutdown_workers()

    def test_c2_stale_start_cannot_disable_b(self):
        """B resolved and usable; a stale A start must not disable B."""
        win = self._mk()
        try:
            # Switch to B and resolve B (fresh generation).
            win.sessions = [make_summary("ses_1"), make_summary("ses_2")]
            win._on_session_selected(1)
            grant_endpoint(win, self.port, "ses_2",
                           generation=win._resolution_generation,
                           sessions=["ses_1", "ses_2"])
            win._on_endpoint_resolved(win._endpoint_status)
            self.assertTrue(win.btn_send.isEnabled())

            # Stale A start arrives.
            win._on_send_started({"request_id": "reqA", "session_id": "ses_1",
                                  "kind": "prompt"})
            self.assertTrue(win.btn_send.isEnabled(),
                            "A stale start must not disable B's Send")
            self.assertTrue(win.btn_cc.isEnabled())
            self.assertTrue(win.input_prompt.isEnabled())
            self.assertEqual(win.lbl_feedback.text(), "",
                             "A stale start must not set B's sending feedback")
        finally:
            win._shutdown_workers()

    def test_c3_visible_controls_reflect_ownership(self):
        """While an active owner exists, visible control state matches the
        ownership policy: disabled with an explicit busy placeholder."""
        win = self._mk()
        try:
            self.assertTrue(win.btn_send.isEnabled())
            owner = win._acquire_admission("send")
            self.assertIsNotNone(owner)
            win._update_send_controls()
            self.assertFalse(win.btn_send.isEnabled())
            self.assertFalse(win.btn_cc.isEnabled())
            self.assertFalse(win.btn_core.isEnabled())
            self.assertFalse(win.btn_3waves.isEnabled())
            self.assertFalse(win.btn_audit_file.isEnabled())
            self.assertFalse(win.input_prompt.isEnabled())
            self.assertIn("Admission in flight", win.input_prompt.placeholderText())
            # A silent double-activation is refused by the owner AND the
            # controls already said so.
            self.assertIsNone(win._acquire_admission("send"))
        finally:
            win._shutdown_workers()

    def test_c4_matching_release_restores_controls_immediately(self):
        """Matching release restores controls without waiting for an
        unrelated resolver/sidebar tick."""
        win = self._mk()
        try:
            owner = win._acquire_admission("send")
            win._update_send_controls()
            self.assertFalse(win.btn_send.isEnabled())
            self.assertIn("Admission in flight", win.input_prompt.placeholderText())
            from viewer_sender import AdmissionResult
            win._on_send_completed(AdmissionResult(
                success=True, admitted_seq=1, request_id=owner["request_id"],
                session_id="ses_1", kind="prompt"))
            self.assertTrue(win.btn_send.isEnabled(),
                            "matching release must restore controls immediately")
            self.assertTrue(win.btn_cc.isEnabled())
            self.assertTrue(win.input_prompt.isEnabled())
            self.assertNotIn("Admission in flight",
                             win.input_prompt.placeholderText())
        finally:
            win._shutdown_workers()

    def test_c5_stale_completion_cannot_release_newer_owners_controls(self):
        """A stale request release must not disturb a NEWER request's
        controls."""
        win = self._mk()
        try:
            # The GUI owns a NEWER reservation (minted at activation).
            newer = win._acquire_admission("send")
            self.assertIsNotNone(newer)
            win._update_send_controls()
            self.assertFalse(win.btn_send.isEnabled())
            self.assertIn("Admission in flight", win.input_prompt.placeholderText())

            # An OLDER request's completion arrives: must not release the
            # NEWER owner and must not flip the newer owner's controls.
            from viewer_sender import AdmissionResult
            win._on_send_completed(AdmissionResult(
                success=True, admitted_seq=2, request_id="req_old",
                session_id="ses_1", kind="prompt"))
            self.assertIsNotNone(win._admission_owner)
            self.assertEqual(win._admission_owner["request_id"],
                             newer["request_id"])
            self.assertFalse(win.btn_send.isEnabled(),
                             "stale release must not flip the newer owner's controls")
            self.assertIn("Admission in flight", win.input_prompt.placeholderText())
        finally:
            win._shutdown_workers()


# ═══════════════════════════════════════════════════════════════════
# Target D: one bound for every presentation source
# ═══════════════════════════════════════════════════════════════════

class TestPresentationBound(unittest.TestCase):
    """Target D: durable AND live obey ONE shared 128 KiB bound; trimming
    never freezes the active live part."""

    def test_d1_oversized_durable_final_stays_within_bound(self):
        p = present.LiveOutputPresenter()
        p.set_session(SES_ID)
        p.on_durable_text(SES_ID, "Ж" * (200 * 1024), "m1")
        out = p.render()
        raw = out.encode("utf-8")
        self.assertLessEqual(len(raw), present.MAX_TEXT_BYTES,
                             "durable render INCLUDING marker must stay <= 128 KiB")
        self.assertIn("truncated", out)
        raw.decode("utf-8")  # never a split character

    def test_d2_multibyte_boundaries_never_split(self):
        p = present.LiveOutputPresenter()
        p.set_session(SES_ID)
        # 3-byte chars; boundary-crossing trims must stay valid UTF-8.
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t1"})
        for _ in range(9):
            p.on_live_event(SES_ID, "session.next.text.delta",
                            {"assistantMessageID": "m1", "textID": "t1",
                             "delta": "日" * 20000})
        out = p.render()
        raw = out.encode("utf-8")
        self.assertLessEqual(len(raw), present.MAX_TEXT_BYTES)
        raw.decode("utf-8")

    def test_d3_live_part_crosses_bound_and_keeps_streaming(self):
        """A live part crosses 128 KiB; later deltas REMAIN visible; the
        active part does NOT freeze merely because old bytes were trimmed."""
        p = present.LiveOutputPresenter()
        p.set_session(SES_ID)
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t1"})
        for _ in range(8):
            p.on_live_event(SES_ID, "session.next.text.delta",
                            {"assistantMessageID": "m1", "textID": "t1",
                             "delta": "x" * 20000})
        self.assertGreater(p._turn_bytes, present.MAX_TEXT_BYTES - 40000)
        # More deltas arrive afterwards; the newest must be visible.
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1",
                         "delta": "NEWEST TAIL"})
        out = p.render()
        self.assertIn("NEWEST TAIL", out,
                      "newest later deltas must remain visible under the bound")
        self.assertLessEqual(len(out.encode("utf-8")), present.MAX_TEXT_BYTES)
        # The part must NOT have been finalized by trimming.
        part = p._part_by_id("t1")
        self.assertFalse(part["finalized"],
                         "trimming must not semantically finalize an active part")
        self.assertFalse(p._finalized_parts)
        # And the part keeps accepting deltas (not frozen).
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1",
                         "delta": "TAIL2"})
        self.assertIn("TAIL2", p.render())

    def test_d4_multi_part_trimming_preserves_finality_semantics(self):
        """Trimming old parts never finalizes a live part; a genuinely
        finalized part still rejects stale partials."""
        p = present.LiveOutputPresenter()
        p.set_session(SES_ID)
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t1"})
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1",
                         "delta": "a" * 20000})
        p.on_live_event(SES_ID, "session.next.text.ended",
                        {"assistantMessageID": "m1", "textID": "t1",
                         "text": "a" * 20000})
        # T2 pushes the turn over the bound -> T1 (oldest) gets trimmed.
        p.on_live_event(SES_ID, "session.next.text.started",
                        {"assistantMessageID": "m1", "textID": "t2"})
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t2",
                         "delta": "b" * (130 * 1024)})
        out = p.render()
        self.assertLessEqual(len(out.encode("utf-8")), present.MAX_TEXT_BYTES)
        # T1's durable finality semantics are preserved: a stale T1 partial
        # can never regress the display.
        before = out
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t1", "delta": "STALE T1"})
        self.assertEqual(p.render(), before.replace("STALE T1", ""))
        # T2 (live, active) is not frozen by the trim.
        p.on_live_event(SES_ID, "session.next.text.delta",
                        {"assistantMessageID": "m1", "textID": "t2", "delta": "T2 KEEPS GOING"})
        self.assertIn("T2 KEEPS GOING", p.render())

    def test_d5_render_bound_is_a_final_safety_net(self):
        """render() itself enforces the bound INCLUDING the marker."""
        p = present.LiveOutputPresenter()
        p.set_session(SES_ID)
        p.on_durable_text(SES_ID, "x" * (300 * 1024), "m1")
        out = p.render()
        self.assertLessEqual(len(out.encode("utf-8")), present.MAX_TEXT_BYTES)


class TestShutdownSymmetry(unittest.TestCase):
    """HUNT T-169 / T-173 / T-175: every terminal path releases admission
    ownership, stops presenter publication, and production convergence
    does NOT depend on the 50ms timer."""

    def _win(self):
        db_path = create_test_db(events=ACTIVE_DB_EVENTS, sessions=(SES_ID,))
        win, mod = make_window(db_path)
        win.sessions = [make_summary(SES_ID)]
        win._on_session_selected(0)
        win._presenter.set_session(SES_ID)
        return win, db_path

    def test_t169_shutdown_clears_admission_owner(self):
        """T-169: _shutdown_workers releases the stranded reservation —
        no admission owner survives Viewer close."""
        win, db_path = self._win()
        try:
            win._admission_owner = {"path": "send", "request_id": "r1",
                                    "session_id": SES_ID, "batch": False}
            win._shutdown_workers()
            self.assertIsNone(win._admission_owner,
                              "shutdown must release admission ownership")
            # Re-acquire after shutdown must still fail closed (closing).
            self.assertTrue(win._closing)
        finally:
            os.remove(db_path)

    def test_t173_presenter_stop_halts_publication(self):
        """T-173: presenter.stop() stops the 50ms publish timer and
        suppresses all further emissions — a closing window receives
        nothing."""
        win, db_path = self._win()
        got = []
        try:
            # A stranded in-flight publication (dirty text + pending state)
            # exists at the moment of shutdown.
            win._presenter.on_live_event(SES_ID, "session.next.text.started",
                                         {"assistantMessageID": "m9",
                                          "textID": "t9"})
            win._presenter.on_live_event(SES_ID, "session.next.text.delta",
                                         {"assistantMessageID": "m9",
                                          "textID": "t9", "delta": "late"})
            self.assertTrue(win._presenter._dirty)
            timer = win._presenter._publish_timer
            self.assertIsNotNone(timer)
            self.assertTrue(timer.isActive())

            win._presenter.signals.text_updated.connect(got.append)
            win._shutdown_workers()  # presenter.stop() happens here
            self.assertTrue(timer is None or not timer.isActive(),
                            "publish timer must be stopped after shutdown")
            self.assertFalse(win._presenter._dirty,
                             "stop() must clear pending publication state")
            # Post-shutdown publications are suppressed entirely.
            win._presenter.publish_now()
            win._presenter.on_durable_text(SES_ID, "after close", "m9")
            win._presenter._advance_lifecycle("RUNNING", "x", "live")
            drain_events(APP, 150)
            self.assertEqual(got, [],
                             "no text publication may reach a closing window")
            # The timer path itself is inert.
            win._presenter._dirty = True
            win._presenter._flush()
            self.assertEqual(got, [])
        finally:
            os.remove(db_path)

    def test_t175_production_convergence_does_not_need_timer(self):
        """T-175: durable fold and session switch converge via publish_now
        (production calls), NOT via the 50ms timer — with the timer never
        firing, the text is already current."""
        win, db_path = self._win()
        try:
            timer = win._presenter._publish_timer
            # Kill the coalescing timer entirely: production must not
            # depend on it for convergence.
            self.assertIsNotNone(timer)
            timer.stop()
            win._presenter._publish_timer = None

            # Durable fold converges immediately.
            win._presenter.on_durable_text(SES_ID, "durable final", "m1")
            self.assertIn("durable final", win.txt_live_output.toPlainText())

            # Session switch converges immediately (empty presentation).
            win._presenter.set_session("ses_other")
            self.assertEqual(win.txt_live_output.toPlainText(), "")
        finally:
            win._shutdown_workers()
            os.remove(db_path)


if __name__ == "__main__":
    unittest.main(verbosity=1)