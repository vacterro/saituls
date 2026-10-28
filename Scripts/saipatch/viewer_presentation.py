"""
viewer_presentation.py - LIVE OUTPUT presentation authority (P0 Target A).

ONE owner reconciles the two text producers (live SSE stream worker and the
durable SQLite observer) before anything reaches the QTextEdit:

    LIVE PARTIAL -> DURABLE FINAL -> DURABLE FINAL REMAINS

Semantics:
- LIVE (streaming): SSE partials render immediately (before SQLite catches up).
- DURABLE: a durable text.ended for the exact matching part
  (assistantMessageID + textID) finalizes THAT PART; afterwards NO stale
  live partial can overwrite it. Finality is PART-SCOPED, never turn-wide:
  one assistant generation may legitimately produce multiple text parts
  (tool activity between them) and a finalized T1 never blocks T2.
- Generation authority: a newer assistantMessageID retires the previous
  generation. Retired generations are remembered (bounded) so a LATE
  durable snapshot for an older generation can never replace a newer live
  generation. A genuinely newer durable generation may still hydrate
  correctly when SSE was missed.
- A non-text SSE event never resurrects stale text.
- Session switch invalidates all presentation state (old-session updates are
  ignored). Stream reconnect cannot resurrect old session/generation text.
- Reasoning is never rendered. ONE 128 KiB total turn bound covers durable
  AND live presentation through a single shared trimming primitive;
  trimming old bytes NEVER semantically finalizes an active live part, so
  the newest tail keeps streaming past the bound.
- SSE lifecycle events (step.started/ended/failed, permission/question/
  input-required) feed activity/state through the queued GUI signal path
  only; SSE NEVER writes to SQLite.

Identity tracked per part:
    session_id, assistantMessageID, textID, source, finalized
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from PyQt6.QtWidgets import QApplication

# Text sources (generation/source identity)
SRC_LIVE = "live"        # SSE token stream
SRC_DURABLE = "durable"  # SQLite observer (reconciliation authority)

# SSE lifecycle events that drive activity/execution state immediacy.
LIFECYCLE_EVENTS = frozenset({
    "session.next.step.started",
    "session.next.step.ended",
    "session.next.step.failed",
    "session.next.permission.asked",
    "session.next.question.asked",
    "session.next.input.required",
})

MAX_TEXT_BYTES = 128 * 1024  # must match observer + live stream bound
TRUNCATION_MARKER = "\n\n[... output truncated at 128 KiB ...]"


# ── Shared trimming primitive (ONE bound for every presentation source) ──

def enforce_utf8_bound(text: str, max_bytes: int = MAX_TEXT_BYTES,
                       marker: str = TRUNCATION_MARKER) -> Tuple[str, bool]:
    """ONE shared presentation bound: return (bounded_text, truncated).

    The rendered UTF-8 output INCLUDING the truncation marker is guaranteed
    <= max_bytes. Old bytes are trimmed first (the newest tail survives);
    a UTF-8 multibyte sequence is never split. This primitive is shared by
    durable AND live presentation so both obey exactly one bound.

    NOTE: this function NEVER mutates or finalizes any live part — the
    caller keeps streaming afterwards.
    """
    marker_b = len(marker.encode("utf-8"))
    budget = max(0, max_bytes - marker_b)
    enc = text.encode("utf-8")
    if len(enc) <= budget:
        return text, False
    cut = enc[len(enc) - budget:]  # keep the NEWEST bytes
    # Advance to the next complete UTF-8 sequence start.
    i = 0
    while i < len(cut) and i < 3:
        try:
            return cut[i:].decode("utf-8") + marker, True
        except UnicodeDecodeError:
            i += 1
    return marker, True


class DurableTextPart:
    """One immutable ordered text part captured atomically by the observer
    at emission time (Target A1): carries its own identity and finality so
    the GUI can reconcile WITHOUT reading any mutable worker field."""
    __slots__ = ("text_id", "text", "finalized")

    def __init__(self, text_id: str, text: str, finalized: bool):
        self.text_id = text_id
        self.text = text
        self.finalized = finalized

    def __eq__(self, other):
        return (isinstance(other, DurableTextPart)
                and self.text_id == other.text_id
                and self.text == other.text
                and self.finalized == other.finalized)

    def __repr__(self):
        return (f"DurableTextPart(text_id={self.text_id!r}, "
                f"bytes={len(self.text.encode('utf-8'))}, "
                f"finalized={self.finalized})")


class DurableTextSnapshot:
    """Immutable durable presentation snapshot (Target A1).

    Captured ATOMICALLY at observer emission time. Carries everything the
    GUI needs to reconcile the durable state: session, assistantMessageID,
    ordered parts (textID + text + finality) and the durable event-sequence
    ordering witness. The GUI thread NEVER reads mutable observer internals
    to reconstruct this.
    """
    __slots__ = ("session_id", "message_id", "parts", "last_event_seq",
                 "captured_at")

    def __init__(self, session_id: str, message_id: Optional[str],
                 parts: Tuple[DurableTextPart, ...], last_event_seq: int):
        self.session_id = session_id
        self.message_id = message_id
        self.parts = tuple(parts)          # frozen tuple of DurableTextPart
        self.last_event_seq = last_event_seq
        self.captured_at = time.time()

    @property
    def assembled_text(self) -> str:
        return "".join(p.text for p in self.parts)

    def __repr__(self):
        return (f"DurableTextSnapshot(session={self.session_id!r}, "
                f"message={self.message_id!r}, parts={len(self.parts)}, "
                f"seq={self.last_event_seq})")


class PresentationSignals(QObject):
    """GUI-facing signals (thread-safe queued delivery)."""
    text_updated = pyqtSignal(str)          # final presentation text
    activity_updated = pyqtSignal(str)      # activity/execution line
    state_updated = pyqtSignal(str)         # execution state (IDLE/RUNNING/...)


class LiveOutputPresenter(QObject):
    """The ONE presentation owner. Observer output AND SSE output both
    enter here; only this owner writes the QTextEdit (via text_updated)."""

    MAX_TEXT_BYTES = MAX_TEXT_BYTES
    TRUNCATION_MARKER = TRUNCATION_MARKER

    # Coalesced publication cadence (ms) — NEVER one repaint per token.
    PUBLISH_CADENCE_MS = 50

    # Bounded history of RETIRED assistant generations: a late durable
    # snapshot for a retired generation can never replace a newer live one.
    MAX_RETIRED_GENERATIONS = 64

    def __init__(self, signals: Optional[PresentationSignals] = None):
        super().__init__()
        self.signals = signals or PresentationSignals()
        self._dirty = False
        self._stopped = False  # set by stop(): suppress ALL later publication
        # Coalesced text publication: live deltas mutate the model cheaply;
        # the QTextEdit write happens at a bounded cadence, matching the
        # stream worker's 30-75 ms UI contract.

        # Current presentation scope
        self._session_id: Optional[str] = None
        self._current_message_id: Optional[str] = None
        self._retired_messages: List[str] = []

        # Ordered turn parts:
        #   {textID, chunks, bytes, source, finalized}
        # source: SRC_LIVE while streaming, SRC_DURABLE once a durable
        # text.ended has finalized the part (a finalized durable part is
        # authoritative and cannot regress to a live partial).
        self._parts: List[Dict[str, Any]] = []
        self._active_part_id: Optional[str] = None
        self._turn_bytes: int = 0
        self._turn_truncated: bool = False

        # PART-SCOPED durable finality (Target A2): (messageID, textID)
        # pairs finalized by a durable reconciliation. A finalized pair
        # rejects stale partials for THAT part only — never a new part of
        # the same assistant generation.
        self._finalized_parts: set = set()

        # ── Lifecycle authority model (Target B) ────────────────────
        # Live SSE lifecycle may advance first; the matching/newer durable
        # lifecycle reconciles; an OLDER durable lifecycle can never
        # regress it. Ordering witnesses: the durable event-sequence barrier
        # (max seq the GUI has actually consumed via event_observed — a
        # snapshot at or below the barrier predates the live-causing event)
        # and the last accepted durable seq.
        self._lifecycle_state: Optional[str] = None      # live+reconciled state
        self._lifecycle_source: Optional[str] = None     # 'live' | 'durable'
        self._lifecycle_seq: int = -1                    # last accepted durable seq
        self._lifecycle_barrier: int = -1                # max consumed durable seq
        self._barrier_at_live: int = -1                  # barrier when live advanced
        self._lifecycle_live_seq: int = 0                # live ordering counter

        if QApplication.instance() is not None:
            self._publish_timer = QTimer(self)
            self._publish_timer.setInterval(self.PUBLISH_CADENCE_MS)
            self._publish_timer.timeout.connect(self._flush)
            self._publish_timer.start()
        else:
            self._publish_timer = None

    # ── Scope / lifecycle ───────────────────────────────────────────

    def set_session(self, session_id: Optional[str]) -> None:
        """Session switch: invalidate ALL old-session presentation state —
        text generations AND the live lifecycle overlay. A reconnect cannot
        resurrect old session/generation text."""
        target = session_id or None
        if target == self._session_id:
            return
        self._session_id = target
        self._reset_turn()
        self._reset_lifecycle()
        # Immediate convergence on session switch: publish the (now empty)
        # presentation synchronously — no 50ms-timer dependence.
        self.publish_now()

    def stop(self) -> None:
        """Shutdown hook: stop the bounded-cadence publication timer and
        suppress ALL further signal emission. A closing window must never
        receive publications after shutdown."""
        self._stopped = True
        self._dirty = False
        if self._publish_timer is not None:
            self._publish_timer.stop()

    def _emit_text(self, text: str) -> None:
        if not self._stopped:
            self.signals.text_updated.emit(text)

    def _emit_state_activity(self, state: str, activity: str) -> None:
        if not self._stopped:
            self.signals.state_updated.emit(state)
            self.signals.activity_updated.emit(activity)

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def current_message_id(self) -> Optional[str]:
        return self._current_message_id

    @property
    def lifecycle_state(self) -> Optional[str]:
        """The current arbitrated execution state for the selected session."""
        return self._lifecycle_state

    def _reset_turn(self) -> None:
        self._current_message_id = None
        self._retired_messages = []
        self._parts = []
        self._active_part_id = None
        self._turn_bytes = 0
        self._turn_truncated = False
        self._finalized_parts = set()
        self._publish()

    def _reset_lifecycle(self) -> None:
        self._lifecycle_state = None
        self._lifecycle_source = None
        self._lifecycle_seq = -1
        self._lifecycle_barrier = -1
        self._barrier_at_live = -1
        self._lifecycle_live_seq = 0
        self._emit_state_activity("IDLE", "")

    def note_durable_seq(self, seq: int) -> None:
        """Record the durable event-sequence progress the GUI has actually
        consumed (fed from observer event_observed). A durable lifecycle
        snapshot at or below this barrier was computed BEFORE the events
        that justify the live overlay and can never regress it."""
        if seq > self._lifecycle_barrier:
            self._lifecycle_barrier = seq

    # ── Durable (SQLite observer) input: reconciliation authority ───

    def on_durable_text(self, session_id: str, text: str,
                        message_id: Optional[str] = None,
                        snapshot: Optional[DurableTextSnapshot] = None) -> None:
        """Durable assembled text from the observer.

        Preferred form: pass the IMMUTABLE DurableTextSnapshot captured by
        the observer (Target A1) — its identity is authoritative. The
        legacy (session_id, text, message_id) signature is retained for
        compatibility; it is treated as a snapshot for the given identity.

        Reconciliation rules (Target A2):
        - The exact part described by the snapshot is finalized; stale
          partials for THAT part can no longer regress it.
        - Other parts of the SAME assistant generation are NOT blocked.
        - A snapshot for a RETIRED (older) generation is ignored: it can
          never replace a newer live generation.
        - A genuinely NEWER durable generation hydrates correctly when SSE
          was missed.
        """
        if session_id != self._session_id or not self._session_id:
            return  # old-session durable update: ignored
        if snapshot is not None and snapshot.session_id != session_id:
            return

        msg_id = snapshot.message_id if snapshot is not None else message_id

        # Parts the observer can actually render: an ACTIVE part carries no
        # durable text (SQLite never stores deltas). An empty durable part
        # must NEVER clobber — or finalize — the live partial that is
        # ahead of the observer.
        durable_parts = tuple(
            p for p in (snapshot.parts if snapshot is not None else ()))
        durable_parts = tuple(p for p in durable_parts if p.text)
        if not text and not durable_parts:
            # Identity-only hydration (active turn, no durable text yet):
            # adopt the snapshot's generation identity ONLY when no live
            # content would be clobbered; a live partial ahead of the
            # observer always wins. A RETIRED identity is stale observer
            # output behind a newer live generation: never revert.
            if msg_id and msg_id != self._current_message_id:
                if msg_id in self._retired_messages:
                    return
                has_live = any(not p["finalized"] and p["bytes"] > 0
                               for p in self._parts)
                if not has_live:
                    if self._current_message_id:
                        self._retired_messages.append(self._current_message_id)
                        if len(self._retired_messages) > self.MAX_RETIRED_GENERATIONS:
                            self._retired_messages.pop(0)
                    self._current_message_id = msg_id
            return

        # Generation authority: retire/adopt exactly once per generation.
        if msg_id and msg_id != self._current_message_id:
            if msg_id in self._retired_messages:
                return  # late durable snapshot for a RETIRED generation
            if self._current_message_id:
                self._retired_messages.append(self._current_message_id)
                if len(self._retired_messages) > self.MAX_RETIRED_GENERATIONS:
                    self._retired_messages.pop(0)
            self._current_message_id = msg_id
            self._parts = []
            self._active_part_id = None
            self._turn_bytes = 0
            self._turn_truncated = False

        if durable_parts:
            # Immutable snapshot carries the ordered parts: they ARE the
            # durable truth (snapshot parts with text are durable-
            # finalized; empty active parts were filtered above).
            self._parts = [{
                "textID": p.text_id,
                "chunks": [p.text],
                "bytes": len(p.text.encode("utf-8")),
                "source": SRC_DURABLE,
                "finalized": True,
            } for p in durable_parts]
            for p in durable_parts:
                if p.finalized:
                    self._finalized_parts.add((msg_id, p.text_id))
            self._turn_bytes = sum(p["bytes"] for p in self._parts)
            self._turn_truncated = False
        else:
            # Legacy single-text form: fold into one durable part.
            bounded, truncated = enforce_utf8_bound(
                text, self.MAX_TEXT_BYTES, self.TRUNCATION_MARKER)
            if self._parts:
                part = self._parts[-1]
                part["chunks"] = [bounded]
                part["bytes"] = len(bounded.encode("utf-8"))
                self._turn_bytes = sum(p["bytes"] for p in self._parts)
            else:
                self._parts = [{
                    "textID": "durable", "chunks": [bounded],
                    "bytes": len(bounded.encode("utf-8")),
                    "source": SRC_DURABLE, "finalized": True,
                }]
                self._turn_bytes = self._parts[0]["bytes"]
            self._turn_truncated = truncated
            part_id = self._parts[-1]["textID"]
            self._finalized_parts.add((msg_id, part_id))
        # Durable fold is an authority-critical transition: converge the
        # presentation IMMEDIATELY (publish_now), never via the 50ms timer.
        self.publish_now()

    # ── Live (SSE) input ────────────────────────────────────────────

    def on_live_event(self, session_id: str, event_type: str,
                      data: Dict[str, Any]) -> None:
        """Live SSE event enters the SAME owner. Only text events may move
        the presentation text; lifecycle events only move activity/state."""
        if session_id != self._session_id or not self._session_id:
            return  # old-session live update: ignored
        msg_id = data.get("assistantMessageID")
        if event_type == "session.next.text.started":
            if isinstance(msg_id, str) and msg_id != self._current_message_id:
                # New assistant generation retires the previous one.
                if self._current_message_id:
                    self._retired_messages.append(self._current_message_id)
                    if len(self._retired_messages) > self.MAX_RETIRED_GENERATIONS:
                        self._retired_messages.pop(0)
                self._current_message_id = msg_id
                self._parts = []
                self._active_part_id = None
                self._turn_bytes = 0
                self._turn_truncated = False
            part_id = data.get("textID")
            if isinstance(part_id, str):
                self._active_part_id = part_id
                if not self._part_by_id(part_id):
                    self._parts.append({
                        "textID": part_id, "chunks": [""], "bytes": 0,
                        "source": SRC_LIVE, "finalized": False,
                    })
        elif event_type == "session.next.text.delta":
            part_id = data.get("textID")
            delta = data.get("delta")
            if not (isinstance(part_id, str) and isinstance(delta, str)):
                return
            if self._current_message_id and isinstance(msg_id, str) \
                    and msg_id != self._current_message_id:
                return  # stale same-generation/old-generation delta ignored
            if (self._current_message_id, part_id) in self._finalized_parts:
                return  # stale partial for an already-finalized part
            part = self._part_by_id(part_id)
            if part is None:
                part = self._part_by_id(self._active_part_id)
            if part is None:
                part = {
                    "textID": part_id, "chunks": [""], "bytes": 0,
                    "source": SRC_LIVE, "finalized": False,
                }
                self._parts.append(part)
            if part.get("finalized"):
                # Stale partial cannot overwrite a finalized part.
                return
            part["chunks"].append(delta)
            added = len(delta.encode("utf-8"))
            part["bytes"] += added
            self._turn_bytes += added
            self._enforce_bound()
        elif event_type == "session.next.text.ended":
            final = data.get("text")
            part_id = data.get("textID")
            if not (isinstance(part_id, str) and isinstance(final, str)):
                return
            if self._current_message_id and isinstance(msg_id, str) \
                    and msg_id != self._current_message_id:
                return  # stale ended for an older generation
            part = self._part_by_id(part_id) or self._part_by_id(
                self._active_part_id)
            if part is None:
                part = {
                    "textID": part_id, "chunks": [""], "bytes": 0,
                    "source": SRC_LIVE, "finalized": False,
                }
                self._parts.append(part)
            # The exact final REPLACES the partial and marks THAT part
            # finalized: later live partials for it cannot regress it. A new
            # part (T2) of the same generation is unaffected.
            self._turn_bytes -= part["bytes"]
            part["chunks"] = [final]
            part["bytes"] = len(final.encode("utf-8"))
            self._turn_bytes += part["bytes"]
            part["source"] = SRC_DURABLE
            part["finalized"] = True
            self._finalized_parts.add((self._current_message_id, part_id))
            self._turn_truncated = False
            self._enforce_bound()
        elif event_type in LIFECYCLE_EVENTS:
            # SSE immediacy for execution/activity only — NEVER SQLite.
            self._apply_lifecycle(event_type, data)
            return
        else:
            # Any other (non-text) SSE event must not restore stale text.
            return
        self._publish()

    # ── Lifecycle authority (Target B) ──────────────────────────────

    def _apply_lifecycle(self, event_type: str, data: Dict[str, Any]) -> None:
        """Thread-safe (queued-signal) activity/state immediacy from SSE."""
        if event_type == "session.next.step.started":
            self._advance_lifecycle("RUNNING", "Running...", "live")
        elif event_type == "session.next.step.ended":
            finish = data.get("finish", "")
            if finish == "stop":
                self._advance_lifecycle("IDLE", "Done", "live")
            elif finish == "tool-calls":
                self._advance_lifecycle("RUNNING", "Tool calls...", "live")
            else:
                self._advance_lifecycle(
                    "IDLE", f"Step ended ({finish})" if finish else "Step ended",
                    "live")
        elif event_type == "session.next.step.failed":
            err = data.get("error", {})
            msg = err.get("message", "Error") if isinstance(err, dict) else "Error"
            self._advance_lifecycle("FAILED", f"FAILED: {str(msg)[:60]}", "live")
        elif event_type in ("session.next.permission.asked",
                            "session.next.question.asked",
                            "session.next.input.required"):
            self._advance_lifecycle("NEEDS_HUMAN", "Waiting for you...", "live")

    def _advance_lifecycle(self, state: str, activity: str,
                           source: str, seq: int = -1) -> None:
        """Lifecycle arbitration (Target B).

        - live advances first (monotone live counter);
        - a durable lifecycle at seq >= the witness that produced the live
          state reconciles (may replace the live overlay with durable truth);
        - an OLDER durable lifecycle can NEVER regress a newer state.
        """
        if source == "live":
            self._lifecycle_live_seq += 1
            self._lifecycle_state = state
            self._lifecycle_source = "live"
            # Anchor the durable-progress barrier AT this live advance: a
            # durable snapshot that reflects NO consumed durable progress
            # beyond this point was computed before the live-causing
            # events existed durably and can never regress this overlay.
            self._barrier_at_live = self._lifecycle_barrier
            self.signals.state_updated.emit(state)
            self.signals.activity_updated.emit(activity)
            return
        # Durable reconciliation: only a witness STRICTLY newer than both
        # the last accepted durable lifecycle AND the consumed-sequence
        # barrier may replace the overlay, AND only once the GUI has
        # actually consumed durable progress (event_observed) beyond the
        # live anchor — the witness then provably includes the events the
        # live overlay described. An older durable state can never regress
        # a newer live state.
        if seq <= max(self._lifecycle_seq, self._lifecycle_barrier):
            return
        if (self._lifecycle_source == "live"
                and self._lifecycle_barrier <= self._barrier_at_live):
            return  # no durable progress observed since the live advance
        self._lifecycle_seq = seq
        self._lifecycle_state = state
        self._lifecycle_source = "durable"
        self.signals.state_updated.emit(state)
        self.signals.activity_updated.emit(activity)

    def on_durable_lifecycle(self, session_id: str, state: str,
                             activity: str = "", seq: int = -1) -> None:
        """A durable (observer/sidebar) lifecycle snapshot for the selected
        session reconciles the live overlay. An older durable snapshot
        cannot regress a newer live state; a matching/newer one may."""
        if session_id != self._session_id or not self._session_id:
            return
        self._advance_lifecycle(state, activity or state, "durable", seq)

    # ── Turn model helpers ──────────────────────────────────────────

    def _part_by_id(self, part_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not part_id:
            return None
        for p in self._parts:
            if p["textID"] == part_id:
                return p
        return None

    def _enforce_bound(self) -> None:
        """ONE total UTF-8 bound across the whole turn (128 KiB), trimming
        oldest parts first, never splitting characters.

        CRITICAL (Target D2): trimming NEVER sets finalized=True on a
        part — old bytes being dropped is not finality. The active live
        part keeps accepting new deltas so the newest bounded tail keeps
        streaming past the limit.
        """
        marker_b = len(self.TRUNCATION_MARKER.encode("utf-8"))
        budget = max(0, self.MAX_TEXT_BYTES - marker_b)
        if self._turn_bytes <= budget:
            return
        self._turn_truncated = True
        for p in self._parts:
            if self._turn_bytes <= budget:
                break
            text = "".join(p["chunks"])
            enc = text.encode("utf-8")
            drop = min(len(enc), self._turn_bytes - budget)
            keep = enc[drop:]
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
            # Deliberately NOT finalized: trimming is not finality.

    # ── Output ──────────────────────────────────────────────────────

    def render(self) -> str:
        text = "".join("".join(p["chunks"]) for p in self._parts)
        if self._turn_truncated:
            text += self.TRUNCATION_MARKER
        # Final safety net: rendered output INCLUDING the marker obeys the
        # single shared bound (Target D1).
        bounded, _ = enforce_utf8_bound(
            text, self.MAX_TEXT_BYTES, self.TRUNCATION_MARKER)
        return bounded

    def _publish(self) -> None:
        """Mark dirty; the bounded-cadence timer performs the actual GUI
        text publication (never one repaint per token)."""
        self._dirty = True

    def _flush(self) -> None:
        if self._stopped:
            return
        if self._dirty:
            self._dirty = False
            self._emit_text(self.render())

    def publish_now(self) -> None:
        """Immediate publication for authority-critical transitions
        (durable finality, session switch). WIRED in production: every
        durable fold and every session switch converges synchronously."""
        self._dirty = False
        self._emit_text(self.render())
