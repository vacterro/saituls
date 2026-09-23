"""
viewer_sender.py - Native V2 HTTP prompt sender. Runs entirely in its own
QThread. All HTTP admission executes in the worker thread — NEVER on the Qt
GUI thread — through a CANCELLABLE transport so Viewer shutdown stays bounded.

Transport: QNetworkAccessManager/QNetworkReply driven by a local QEventLoop
with a timeout. A queued stop aborts the in-flight reply from the worker's
own thread, so shutdown can never hang on a stalled urlopen-style read.
Closing the Viewer may cancel Viewer-side pending admission work but NEVER
sends any OpenCode abort/interrupt — an admission the server accepted stays
accepted.

Request correlation (P0): every send carries a unique request_id plus
session_id and kind; AdmissionResult echoes them so the GUI can discard
results that belong to an older selection.
"""
import json
import itertools

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import (
    QObject, pyqtSignal, pyqtSlot, QUrl, QEventLoop, QTimer, QByteArray,
)
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

try:
    import psutil
except ImportError:
    psutil = None

try:
    import viewer_presets
except ImportError:
    viewer_presets = None

REQUEST_TIMEOUT_MS = 10000  # mirrors the documented 10s admission bound
_inflight_loop: Optional[QEventLoop] = None
_inflight_reply: Optional[QNetworkReply] = None


@dataclass
class AdmissionResult:
    success: bool
    admitted_seq: Optional[int] = None
    error: Optional[str] = None
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    kind: Optional[str] = None  # 'prompt' | 'audit' | '3-waves' | 'cc'


class SenderSignals(QObject):
    send_started = pyqtSignal(object)    # request envelope dict
    send_completed = pyqtSignal(object)  # AdmissionResult
    waves_progress = pyqtSignal(object)  # dict(request_id, kind, message)
    waves_completed = pyqtSignal(object) # AdmissionResult (aggregated)
    waves_failed = pyqtSignal(object)    # AdmissionResult


class NativeSender(QObject):
    # Queued command signals (GUI -> worker). Connected AFTER moveToThread.
    # The GUI mints request_id SYNCHRONOUSLY at user activation (single-
    # flight admission ownership) and passes it with the dispatch so the
    # worker's correlation identity matches the GUI reservation.
    send_prompt_cmd = pyqtSignal(str, str, str, str)  # url, session, text, request_id
    send_audit_cmd = pyqtSignal(str, str, str, str)  # url, session, text, kind
    # The 3-wave BATCH request_id is minted at GUI activation; one
    # reservation owns the complete CORE/W2/PERF batch.
    send_three_waves_cmd = pyqtSignal(str, str, str)  # url, session, batch_request_id
    # Immutable audit request envelope + prepared text. The worker
    # REVALIDATES the captured target (pid/start-time alive, exact session
    # still on the captured endpoint) before POSTing and fails closed on any
    # authority change — never reroutes to the currently selected session.
    send_verified_cmd = pyqtSignal(object, str)      # request dict, text
    stop_worker = pyqtSignal()

    _ids = itertools.count(1)

    VERIFY_TIMEOUT_MS = 3000

    def __init__(self):
        super().__init__()
        self.signals = SenderSignals()
        self._nam: Optional[QNetworkAccessManager] = None
        self._stopping = False

    # ── Request identity ────────────────────────────────────────────

    @staticmethod
    def new_request_id() -> str:
        return f"req_{next(NativeSender._ids)}"

    # ── Queued command slots (worker thread) ────────────────────────

    @pyqtSlot(str, str, str, str)
    def _on_send_prompt(self, endpoint_url: str, session_id: str, text: str,
                        request_id: str = ""):
        if not request_id:
            request_id = self.new_request_id()
        kind = "cc" if text == "cc" else "prompt"
        self.signals.send_started.emit({
            "request_id": request_id, "session_id": session_id, "kind": kind,
            "gui_minted": bool(request_id)})
        result = self._send_one(endpoint_url, session_id, text,
                                request_id=request_id, kind=kind)
        self.signals.send_completed.emit(result)

    @pyqtSlot(str, str, str, str)
    def _on_send_audit(self, endpoint_url: str, session_id: str, text: str, kind: str):
        request_id = self.new_request_id()
        self.signals.send_started.emit({
            "request_id": request_id, "session_id": session_id, "kind": kind})
        result = self._send_one(endpoint_url, session_id, text,
                                request_id=request_id, kind=kind)
        self.signals.send_completed.emit(result)

    @pyqtSlot(str, str, str)
    def _on_send_3_waves(self, endpoint_url: str, session_id: str,
                         batch_request_id: str = ""):
        if not batch_request_id:
            batch_request_id = self.new_request_id()
        if not viewer_presets:
            self.signals.waves_failed.emit(AdmissionResult(
                success=False, error="viewer_presets module is missing",
                request_id=batch_request_id, session_id=session_id,
                kind="3-waves"))
            return

        request_id = batch_request_id
        texts = []
        for slot in ("core", "wave2", "performance"):
            try:
                text = viewer_presets.read_preset_text(slot)
            except ValueError as e:
                self.signals.waves_failed.emit(AdmissionResult(
                    success=False, error=str(e), request_id=request_id,
                    session_id=session_id, kind="3-waves"))
                return
            if text is None:
                self.signals.waves_failed.emit(AdmissionResult(
                    success=False, error=f"Missing preset: {slot}",
                    request_id=request_id, session_id=session_id, kind="3-waves"))
                return
            texts.append((slot.upper(), text))

        for name, text in texts:
            label = {"CORE": "CORE", "WAVE2": "W2", "PERFORMANCE": "PERF"}[name]
            self.signals.waves_progress.emit({
                "request_id": request_id, "session_id": session_id,
                "kind": "3-waves", "message": f"Sending {label}..."})
            result = self._send_one(endpoint_url, session_id, text,
                                    request_id=request_id, kind="3-waves")
            if not result.success:
                self.signals.waves_failed.emit(AdmissionResult(
                    success=False,
                    error=f"{label} failed: {result.error}",
                    request_id=request_id, session_id=session_id, kind="3-waves"))
                return
            self.signals.waves_progress.emit({
                "request_id": request_id, "session_id": session_id,
                "kind": "3-waves",
                "message": f"Queued {label} #{result.admitted_seq}"})

        self.signals.waves_completed.emit(AdmissionResult(
            success=True, error=None, request_id=request_id,
            session_id=session_id, kind="3-waves"))

    @pyqtSlot(object, str)
    def _on_send_verified(self, request: dict, text: str):
        """Asynchronously prepared audit send: revalidate the CAPTURED
        target, then POST. Any authority change fails that request visibly.
        No reroute to the currently selected session; no SQLite fallback."""
        request = dict(vars(request)) if hasattr(request, "__dataclass_fields__") \
            else dict(request or {})
        rid = request.get("request_id") or self.new_request_id()
        sid = request.get("session_id") or ""
        kind = request.get("kind") or "audit"
        err = self._verify_target(request)
        if err is not None:
            self.signals.send_completed.emit(AdmissionResult(
                success=False, error=err, request_id=rid,
                session_id=sid, kind=kind))
            return
        result = self._send_one(request.get("endpoint_url") or "", sid, text,
                                request_id=rid, kind=kind)
        self.signals.send_completed.emit(result)

    def _verify_target(self, request: dict) -> Optional[str]:
        """Pre-admission revalidation of an immutable request envelope.

        1. The captured PID/start-time identity is still alive.
        2. The exact captured session still exists on the captured endpoint
           (GET /api/session/{id}).

        Returns an error string on any authority change, None when the
        captured target may still receive the prepared text.
        """
        endpoint_url = request.get("endpoint_url") or ""
        session_id = request.get("session_id") or ""
        pid = request.get("endpoint_pid")
        start_time = request.get("endpoint_start_time")

        if not endpoint_url or not session_id:
            return ("Captured request carries no endpoint/session authority; "
                    "audit request failed closed")

        if psutil is not None and pid is not None:
            try:
                proc = psutil.Process(int(pid))
                if not proc.is_running():
                    return (f"Endpoint process {pid} is no longer alive; "
                            "audit request failed closed")
                if start_time is not None and abs(
                        proc.create_time() - float(start_time)) > 1.0:
                    return (f"Endpoint pid {pid} was recycled (different "
                            "process identity); audit request failed closed")
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess, ValueError):
                return (f"Endpoint process {pid} is no longer verifiable; "
                        "audit request failed closed")

        body, err = self._get_json_cancellable(
            f"{endpoint_url}/api/session/{session_id}")
        if err is not None:
            return (f"Endpoint verify failed: {err}; "
                    "audit request failed closed")
        if not isinstance(body, dict):
            return "Endpoint verify failed: unexpected response"
        from viewer_endpoint import extract_session_ids
        if session_id not in extract_session_ids(body):
            return (f"Captured session {session_id} no longer exists on the "
                    "captured endpoint; audit request failed closed")
        return None

    def _get_json_cancellable(self, url: str):
        """Cancellable bounded GET via QNetworkReply (worker thread).
        Returns (dict_or_None, error_or_None)."""
        global _inflight_loop, _inflight_reply
        if self._nam is None:
            self._nam = QNetworkAccessManager(self)
        request = QNetworkRequest(QUrl(url))
        request.setTransferTimeout(self.VERIFY_TIMEOUT_MS)
        reply = self._nam.get(request)
        _inflight_reply = reply

        loop = QEventLoop()
        _inflight_loop = loop
        finished = {"done": False}

        def on_finished():
            finished["done"] = True
            loop.quit()

        reply.finished.connect(on_finished)
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        watchdog.start(self.VERIFY_TIMEOUT_MS + 1000)
        if self._stopping:
            loop.quit()
        loop.exec()

        _inflight_loop = None
        _inflight_reply = None
        try:
            watchdog.stop()
        except RuntimeError:
            pass

        if not finished["done"]:
            try:
                reply.abort()
            except RuntimeError:
                pass
            reply.deleteLater()
            return None, "verify cancelled"

        if reply.error() != QNetworkReply.NetworkError.NoError:
            error_int = int(getattr(reply.error(), "value", reply.error()))
            err = f"HTTP Error {error_int}: {reply.errorString()}"
            reply.deleteLater()
            return None, err
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        reply.deleteLater()
        if status != 200:
            return None, f"HTTP Error {status}"
        try:
            return json.loads(raw.decode("utf-8")), None
        except (ValueError, UnicodeDecodeError):
            return None, "invalid JSON"

    @pyqtSlot()
    def _on_stop(self):
        """Queued stop: abort any in-flight reply from this thread so
        shutdown stays bounded. Sends NOTHING to OpenCode."""
        self._stopping = True
        reply = _inflight_reply
        loop = _inflight_loop
        if reply is not None:
            try:
                reply.abort()
            except RuntimeError:
                pass
        if loop is not None:
            loop.quit()

    def stop(self):
        self._on_stop()

    # ── Cancellable transport (worker thread) ───────────────────────

    def _post(self, endpoint_url: str, session_id: str, text: str):
        """POST one admission via QNetworkReply with a bounded, abortable
        event-loop wait. Returns (payload_dict_or_None, error_or_None)."""
        global _inflight_loop, _inflight_reply
        if self._nam is None:
            self._nam = QNetworkAccessManager(self)
        url = f"{endpoint_url}/api/session/{session_id}/prompt"
        payload = {"prompt": {"text": text}, "delivery": "queue"}
        body = QByteArray(json.dumps(payload).encode("utf-8"))

        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"Content-Type", b"application/json")
        request.setTransferTimeout(REQUEST_TIMEOUT_MS)
        reply = self._nam.post(request, body)
        _inflight_reply = reply

        loop = QEventLoop()
        _inflight_loop = loop
        finished = {"done": False}

        def on_finished():
            finished["done"] = True
            loop.quit()

        reply.finished.connect(on_finished)
        # Hard bound independent of transferTimeout for older Qt builds.
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        watchdog.start(REQUEST_TIMEOUT_MS + 1000)
        if self._stopping:
            loop.quit()
        loop.exec()

        _inflight_loop = None
        _inflight_reply = None
        try:
            watchdog.stop()
        except RuntimeError:
            pass

        if not finished["done"]:
            # Stopped or timed out: abort locally; sends nothing to OpenCode.
            try:
                reply.abort()
            except RuntimeError:
                pass
            reply.deleteLater()
            return None, "request cancelled"

        error = reply.error()
        error_int = int(getattr(error, "value", error))
        if error != QNetworkReply.NetworkError.NoError:
            err_msg = f"HTTP Error {error_int}: {reply.errorString()}"
            body_bytes = bytes(reply.readAll())
            if body_bytes:
                try:
                    err_msg += f" - {body_bytes.decode('utf-8')}"
                except UnicodeDecodeError:
                    pass
            reply.deleteLater()
            return None, err_msg

        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        reply.deleteLater()
        if status != 200:
            return None, f"HTTP Error {status}: {reply.errorString()}"
        try:
            resp = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, "Unexpected response format (invalid JSON)"
        return resp, None

    # ── HTTP transport (worker thread only) ─────────────────────────

    def _send_one(self, endpoint_url: str, session_id: str, text: str,
                  request_id: Optional[str] = None,
                  kind: str = "prompt") -> AdmissionResult:
        resp, err = self._post(endpoint_url, session_id, text)
        if err is not None:
            return AdmissionResult(success=False, error=err,
                                   request_id=request_id,
                                   session_id=session_id, kind=kind)
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict) and "admittedSeq" in data:
            return AdmissionResult(success=True, admitted_seq=data["admittedSeq"],
                                   request_id=request_id,
                                   session_id=session_id, kind=kind)
        return AdmissionResult(success=False,
                               error="Unexpected response format (missing admittedSeq)",
                               request_id=request_id,
                               session_id=session_id, kind=kind)

    # ── Direct-call work methods (tests only; production uses slots) ─

    def send_prompt(self, endpoint_url: str, session_id: str, text: str,
                    request_id: str = ""):
        request_id = request_id or self.new_request_id()
        kind = "cc" if text == "cc" else "prompt"
        self.signals.send_started.emit({
            "request_id": request_id, "session_id": session_id, "kind": kind,
            "gui_minted": False})
        result = self._send_one(endpoint_url, session_id, text,
                                request_id=request_id, kind=kind)
        self.signals.send_completed.emit(result)
        return result

    def send_cc(self, endpoint_url: str, session_id: str):
        return self.send_prompt(endpoint_url, session_id, "cc")

    def send_verified(self, request: dict, text: str):
        """Direct-call verified send (tests only)."""
        self._on_send_verified(request, text)

    def send_audit(self, endpoint_url: str, session_id: str, slot: str):
        if not viewer_presets:
            result = AdmissionResult(success=False,
                                     error="viewer_presets module is missing",
                                     request_id=self.new_request_id(),
                                     session_id=session_id, kind="audit")
            self.signals.send_completed.emit(result)
            return result
        text = viewer_presets.read_preset_text(slot)
        if text is None:
            result = AdmissionResult(success=False,
                                     error=f"Preset not configured for slot '{slot}'",
                                     request_id=self.new_request_id(),
                                     session_id=session_id, kind="audit")
            self.signals.send_completed.emit(result)
            return result
        return self.send_prompt(endpoint_url, session_id, text)

    def send_3_waves(self, endpoint_url: str, session_id: str,
                     batch_request_id: str = ""):
        batch_request_id = batch_request_id or self.new_request_id()
        if not viewer_presets:
            self.signals.waves_failed.emit(AdmissionResult(
                success=False, error="viewer_presets module is missing",
                request_id=batch_request_id, session_id=session_id,
                kind="3-waves"))
            return None
        texts = []
        for slot in ("core", "wave2", "performance"):
            text = viewer_presets.read_preset_text(slot)
            if text is None:
                self.signals.waves_failed.emit(AdmissionResult(
                    success=False, error=f"Missing preset: {slot}",
                    request_id=batch_request_id, session_id=session_id,
                    kind="3-waves"))
                return None
            texts.append((slot.upper(), text))
        request_id = batch_request_id
        for name, text in texts:
            label = {"CORE": "CORE", "WAVE2": "W2", "PERFORMANCE": "PERF"}[name]
            self.signals.waves_progress.emit({
                "request_id": request_id, "session_id": session_id,
                "kind": "3-waves", "message": f"Sending {label}..."})
            result = self._send_one(endpoint_url, session_id, text,
                                    request_id=request_id, kind="3-waves")
            if not result.success:
                self.signals.waves_failed.emit(AdmissionResult(
                    success=False, error=f"{label} failed: {result.error}",
                    request_id=request_id, session_id=session_id, kind="3-waves"))
                return result
            self.signals.waves_progress.emit({
                "request_id": request_id, "session_id": session_id,
                "kind": "3-waves",
                "message": f"Queued {label} #{result.admitted_seq}"})
        completed = AdmissionResult(success=True, request_id=request_id,
                                    session_id=session_id, kind="3-waves")
        self.signals.waves_completed.emit(completed)
        return completed
