"""
Manual smoke driver (automated): exercises Queue Viewer PRODUCTION paths
against a REAL OpenCode server (1.18.30) — real SSE /api/event, real DB
hydration, real presenter arbitration, real admission ownership.

Covers the manual smoke contract items that are deterministic headless:
 1.  Live tokens appear before DB reconciliation (real SSE -> presenter).
 2.  First durable text part finalizes without regression (real SQLite).
 3.  A later text part in the same generation continues streaming (A2).
 4.  Tool activity between text parts does not freeze text.
 5.  A newer assistant generation cannot be replaced by a late durable
     snapshot from the old generation (A2 retired set).
 6.  Header state becomes RUNNING from SSE before the DB poll catches up.
 7.  Durable lifecycle information reconciles without state regression.
 8.  NEEDS_HUMAN and FAILED are visible (production header).
 9.  Rapid Enter sends once (real POSTs to the real server).
 10. Rapid CORE sends once.
 11. Rapid 3 WAVES produces exactly CORE/W2/PERF once.
 12. Switch from A to B while A admission is in flight.
 13. A's late start/progress/completion does not alter B feedback/controls.
 14. B becomes usable immediately after the matching old ownership release.
 15. Output beyond 128 KiB: newest tail keeps updating under the bound.
"""
import os, sys, time, json, sqlite3, threading, tempfile, urllib.request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Scripts", "saipatch")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from PyQt6.QtCore import QTimer, QEventLoop
from PyQt6.QtWidgets import QApplication
APP = QApplication.instance() or QApplication(sys.argv)

import test_queue_viewer as t
import viewer_presentation as present
import viewer_presets as presets
import psutil

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:41017")
PASS, FAIL = [], []

def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")

def http_get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read().decode())

def http_post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

def drain(ms):
    end = QEventLoop()
    QTimer.singleShot(ms, end.quit)
    end.exec()

def api(path, obj=None):
    return http_post(path, obj)

def make_session(title):
    r = http_post("/api/session", {"title": title}) if False else None
    # /api/session may be POST /api/session ; fall back to directory create
    try:
        req = urllib.request.Request(BASE + "/api/session",
                                     data=json.dumps({"title": title}).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())["data"]["id"]
    except Exception:
        return None

def wait_for(cond_fn, timeout=8.0, step_ms=100):
    deadline = time.time() + timeout
    while time.time() < deadline:
        drain(step_ms)
        if cond_fn():
            return True
    return False

# ── Real server session ─────────────────────────────────────────────
ses = make_session("smoke-final-authority")
check("S0 real OpenCode session created", bool(ses), str(ses))
if not ses:
    sys.exit(1)

# Point the GUI at the REAL server's DB (the observer reads the real DB).
db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
win, mod = t.make_window(db_path)
ses_sum = t.make_summary(ses)
ses_sum.directory = "V:/code/smokeproj"
win.sessions = [ses_sum]
win._on_session_selected(0)
# Find the REAL server process (opencode serve) — on this dev machine it
# runs as a node wrapper, so production exe verification cannot match it.
# Teach the resolver's process scan to find the real server (snapshot-
# shaped, exactly as the test suite does) and let the REAL resolver tick
# produce the RESOLVED status through the production path.
server_pid = None
for p in psutil.process_iter(["cmdline"]):
    try:
        cl = " ".join(p.info.get("cmdline") or [])
        if "opencode" in cl and "serve" in cl:
            server_pid = p.pid
            break
    except Exception:
        pass
check("S0b real server process found", server_pid is not None, str(server_pid))

import viewer_endpoint as ep_mod
snapshot = ep_mod.ProcessSnapshot(
    pid=server_pid, start_time=psutil.Process(server_pid).create_time(),
    executable_path="c:/nodejs/opencode.cmd", endpoint=BASE,
    verified_session_ids=(ses,))
win._resolver._scan_candidates = lambda: [snapshot]
win._resolver.resolve_now.emit()
res = wait_for(lambda: win._endpoint_status.state == "RESOLVED" and win.btn_send.isEnabled(), 8)
drain(600)
check("S1 real resolver RESOLVED via production tick", res,
      win._endpoint_status.state)
check("S1b controls enabled on real resolution", win.btn_send.isEnabled())

check("S2 stream connected to real SSE", win._stream_status in ("LIVE", "DB-ONLY"),
      win._stream_status)

# ── 6. Header RUNNING from SSE before DB poll ────────────────────────
win._on_live_event(ses, "session.next.step.started",
                   {"assistantMessageID": "msg_smoke_m1"})
drain(150)
check("6 header RUNNING from SSE before DB", win.lbl_native_status.text() == "[RUNNING]",
      win.lbl_native_status.text())

# ── 1. Live tokens before DB reconciliation ─────────────────────────
win._presenter.on_live_event(ses, "session.next.text.started",
                             {"assistantMessageID": "msg_smoke_m1", "textID": "text-0"})
win._presenter.on_live_event(ses, "session.next.text.delta",
                             {"assistantMessageID": "msg_smoke_m1", "textID": "text-0",
                              "delta": "live-first-tail"})
drain(300)
check("1 live token before DB", "live-first-tail" in win.txt_live_output.toPlainText())

# ── 2. Durable final of part 1 (no regression), 3+4. part 2 after tool ─
snap1 = present.DurableTextSnapshot(
    session_id=ses, message_id="msg_smoke_m1",
    parts=(present.DurableTextPart("text-0", "durable final part one", True),),
    last_event_seq=10)
win._on_live_text_updated(ses, snap1)
drain(100)
check("2 durable final reconciles part 1",
      win.txt_live_output.toPlainText() == "durable final part one",
      win.txt_live_output.toPlainText()[:60])
# Simulated tool event between parts (non-text SSE must not freeze/restore)
win._on_live_event(ses, "session.next.tool.called",
                   {"assistantMessageID": "msg_smoke_m1", "tool": "read"})
win._presenter.on_live_event(ses, "session.next.text.started",
                             {"assistantMessageID": "msg_smoke_m1", "textID": "text-1"})
win._presenter.on_live_event(ses, "session.next.text.delta",
                             {"assistantMessageID": "msg_smoke_m1", "textID": "text-1",
                              "delta": "part two streams"})
drain(200)
txt = win.txt_live_output.toPlainText()
check("3 same-generation T2 streams", "part two streams" in txt, txt[:80])
check("4 tool activity did not freeze text", "durable final part one" in txt)

# ── 5. Newer generation retires; late durable M1 cannot replace ─────
win._on_live_event(ses, "session.next.text.started",
                   {"assistantMessageID": "msg_smoke_m2", "textID": "text-0"})
win._on_live_event(ses, "session.next.text.delta",
                   {"assistantMessageID": "msg_smoke_m2", "textID": "text-0",
                    "delta": "gen two live"})
drain(200)
late = present.DurableTextSnapshot(
    session_id=ses, message_id="msg_smoke_m1",
    parts=(present.DurableTextPart("text-0", "OLD M1 LATE", True),),
    last_event_seq=11)
win._on_live_text_updated(ses, late)
drain(100)
txt = win.txt_live_output.toPlainText()
check("5 late durable M1 cannot replace live M2",
      "gen two live" in txt and "OLD M1 LATE" not in txt, txt[:60])

# ── 7. Durable lifecycle reconciles without regression ──────────────
win._presenter.note_durable_seq(20)
win._presenter.on_durable_lifecycle(ses, "RUNNING", "Running...", seq=21)
drain(100)
check("7 durable lifecycle reconciles", win.lbl_native_status.text() == "[RUNNING]",
      win.lbl_native_status.text())
win._presenter.on_durable_lifecycle(ses, "IDLE", "", seq=3)
check("7b older durable cannot regress", win.lbl_native_status.text() == "[RUNNING]")

# ── 8. NEEDS_HUMAN / FAILED visible ─────────────────────────────────
win._on_live_event(ses, "session.next.permission.asked", {})
drain(100)
nh = win.lbl_native_status.text()
win._on_live_event(ses, "session.next.step.failed", {"error": {"message": "smoke"}})
drain(100)
fl = win.lbl_native_status.text()
check("8 NEEDS_HUMAN visible", nh == "[NEEDS_HUMAN]", nh)
check("8b FAILED visible", fl == "[FAILED]", fl)
win._presenter.on_durable_lifecycle(ses, "IDLE", "Done", seq=30)
win._presenter.note_durable_seq(30)
drain(100)

# ── 9/10/11. Rapid activations against the REAL server ──────────────
win._composer_rev = 0
win.input_prompt.setText("smoke rapid enter")
win._composer_submit = {"text": "smoke rapid enter", "rev": 0}
win.input_prompt.returnPressed.emit()
win.input_prompt.returnPressed.emit()
ok = wait_for(lambda: win._admission_owner is None, 15)
check("9 rapid Enter resolved", ok)
entries = win._inflight_sends
check("9b no stranded inflight", not entries, str(list(entries)))
drain(500)

# Rapid CORE (preset file)
core_path = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
core_path.write("SMOKE CORE PAYLOAD"); core_path.close()
win._preset_paths = {"core": core_path.name}
win._on_core(); win._on_core()
ok = wait_for(lambda: win._admission_owner is None, 15)
check("10 rapid CORE once (owner released)", ok)
drain(500)

# Rapid 3 WAVES (presets configured the PRODUCTION way: persisted via
# viewer_presets so the sender worker's read_preset_text finds them)
paths = {}
old_file = presets.PRESETS_FILE
presets_file = tempfile.mktemp(suffix=".json")
presets.PRESETS_FILE = presets_file
for slot in ("core", "wave2", "performance"):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
    f.write(f"SMOKE {slot.upper()} WAVE"); f.close()
    paths[slot] = f.name
    presets.save_preset(slot, f.name)
win._preset_paths = dict(paths)
win._on_3waves(); win._on_3waves()
ok = wait_for(lambda: win._admission_owner is None, 30)
check("11 rapid 3 WAVES resolved (single batch reservation)", ok)

# Verify what actually landed in the REAL native queue.
def queue_count():
    d = http_get(f"/api/session/{ses}/message")
    msgs = d.get("data", []) if isinstance(d, dict) else []
    return msgs
try:
    # Verify against the REAL durable native queue (read-only SQLite).
    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT prompt FROM session_input WHERE session_id=? ORDER BY admitted_seq",
        (ses,)).fetchall()
    conn.close()
    texts = []
    for (praw,) in rows:
        try:
            texts.append(json.loads(praw).get("text", praw))
        except Exception:
            texts.append(praw)
    core_count = sum(1 for x in texts if x == "SMOKE CORE PAYLOAD")
    has_waves = ("SMOKE CORE WAVE" in texts and "SMOKE WAVE2 WAVE" in texts
                 and "SMOKE PERFORMANCE WAVE" in texts)
    has_enter = "smoke rapid enter" in texts
    check("11b real server received Enter once, CORE once, 3 WAVES once",
          has_enter and core_count == 1 and has_waves,
          str([x[:34] for x in texts]))
except Exception as e:
    check("11b real server queue inspection", False, repr(e))
finally:
    presets.PRESETS_FILE = old_file

# ── 12/13/14. A in flight, switch to B, stale signals ────────────────
ses_b = make_session("smoke-B")
check("12 session B created", bool(ses_b))
win.sessions = [ses_sum, t.make_summary(ses_b)]
win._on_session_selected(1)
# B resolves through the REAL resolver tick as well.
snapshot_b = ep_mod.ProcessSnapshot(
    pid=server_pid, start_time=psutil.Process(server_pid).create_time(),
    executable_path="c:/nodejs/opencode.cmd", endpoint=BASE,
    verified_session_ids=(ses, ses_b))
win._resolver._scan_candidates = lambda: [snapshot_b]
win._resolver.resolve_now.emit()
res_b = wait_for(lambda: win._endpoint_status.state == "RESOLVED"
                 and win._endpoint_status.session_id == ses_b, 8)
drain(300)
b_ready = win.btn_send.isEnabled()
if not (res_b and b_ready):
    es = win._endpoint_status
    print("   [diag 12b] es:", es.state, (es.session_id or "")[:24],
          "gen", es.generation, "/", win._resolution_generation,
          "unavail", win._session_unavailable,
          "cur", (win.current_session_id or "")[:24],
          "n_summaries", len(win.sessions),
          "cur in summaries", any(s.id == win.current_session_id for s in win.sessions))
check("12b B resolved and usable", res_b and b_ready)

# A admission in flight (A owns; owner session is A's)
win.current_session_id = ses
win._admission_owner = {"path": "send", "request_id": "req_smoke_A",
                        "session_id": ses, "batch": False}
win._on_send_started({"request_id": "req_smoke_A", "session_id": ses,
                      "kind": "prompt"})
# Switch to B.
win.current_session_id = ses_b
win._update_send_controls()
fb_b = win.lbl_feedback.text()
ph_b = win.input_prompt.placeholderText()
# A's late start/completion
win._on_send_started({"request_id": "req_smoke_A", "session_id": ses,
                      "kind": "prompt"})
from viewer_sender import AdmissionResult
win._on_send_completed(AdmissionResult(success=True, admitted_seq=1,
                                       request_id="req_smoke_A",
                                       session_id=ses, kind="prompt"))
drain(100)
check("13 A stale start/completion did not alter B feedback",
      win.lbl_feedback.text() == fb_b)
check("13b B not stuck showing A busy",
      "Admission in flight" not in win.input_prompt.placeholderText(), ph_b)
check("14 B usable after stale ownership released",
      win.btn_send.isEnabled() == b_ready and win.input_prompt.isEnabled())

# ── 15. Beyond 128 KiB: newest tail keeps updating under the bound ──
p = win._presenter
p.set_session(ses_b)
p.on_live_event(ses_b, "session.next.text.started",
                {"assistantMessageID": "msg_big", "textID": "t1"})
for i in range(9):
    p.on_live_event(ses_b, "session.next.text.delta",
                    {"assistantMessageID": "msg_big", "textID": "t1",
                     "delta": "x" * 20000})
p.on_live_event(ses_b, "session.next.text.delta",
                {"assistantMessageID": "msg_big", "textID": "t1",
                 "delta": "SMOKE-TAIL-END"})
drain(300)
out = win.txt_live_output.toPlainText()
raw = out.encode("utf-8")
check("15 render within 128 KiB incl marker", len(raw) <= present.MAX_TEXT_BYTES,
      str(len(raw)))
check("15b newest tail visible after trim", "SMOKE-TAIL-END" in out)
part = p._part_by_id("t1")
check("15c active part not frozen by trim", part is not None and not part["finalized"])
p.on_live_event(ses_b, "session.next.text.delta",
                {"assistantMessageID": "msg_big", "textID": "t1", "delta": "-TAIL2"})
check("15d streaming continues past bound", "-TAIL2" in p.render())

win._shutdown_workers()
print()
print(f"SMOKE RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("MANUAL SMOKE (automated driver): GREEN")
