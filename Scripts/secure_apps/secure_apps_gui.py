"""SAITULS Secure Apps window.

The human-facing Secure Apps control surface built on top of the FIDO2 broker.
This module is the ONE GUI implementation; ``secure_apps.pyw`` is a launcher
that only puts this directory on ``sys.path`` and calls :func:`main`.

Win95 Golden Default visual contract (UI.md) -- three primary tabs:
1. Apps: Everyday operational surface (Open, Lock now, Default/Aggressive modes).
2. Setup & Security: Guided 8-step checklist and single next action engine.
3. Recovery: Enrolled key management, storage/migration recovery, emergency guides.

Threading contract, because every defect this surface ever had lived here:
the window's thread owns every widget and every dialog, and the broker /
acceptance workers own the heavyweight calls. Anything a worker needs from a
person -- a PIN, a security-relevant yes/no, a physical instruction -- is a
token handed over a queued signal and answered back through a ``queue.Queue``.
No worker ever touches a ``QWidget``, and no security question is ever
answered on the operator's behalf: an unanswered question is a NO.
"""
import ntpath
import os
import queue
import subprocess
import sys
import threading
import time

HERE = ntpath.dirname(ntpath.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PyQt6.QtCore import (
    Qt, QObject, QThread, QTimer, pyqtSignal, pyqtSlot, QAbstractNativeEventFilter
)
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QAbstractItemView, QDialog, QDialogButtonBox, QPlainTextEdit, QLineEdit,
    QMessageBox, QListWidget, QSizePolicy, QTabWidget, QRadioButton, QButtonGroup,
    QScrollArea, QFileDialog
)

import sa_acceptance
import sa_acceptance_runner
import sa_audit
import sa_auth
import sa_broker
import sa_config
import sa_crypto
import sa_enroll
import sa_migrate
import sa_privtask
import sa_setup
import sa_state

# ── Golden Default palette (UI.md) ─────────────────────────────────
C_BG = "#1A1810"
C_BG_SOFT = "#232018"
C_SURFACE = "#332E22"
C_RAISED = "#3D372A"
C_ALT = "#453D30"
C_BORDER_DARK = "#100E08"
C_BORDER_HL = "#F0D060"
C_BEVEL_LIGHT = "#75663D"
C_BORDER_MUTED = "#5A5040"
C_TEXT = "#D4C89A"
C_TEXT2 = "#9C9371"
C_MUTED = "#6E674E"
C_DANGER = "#7A2020"
C_DANGER_TEXT = "#D66464"
C_WARNING = "#7A7A20"
C_SELECTION = "#3D372A"
C_COMPARE = "#14120C"

QSS = """
* { font-family: 'Verdana', sans-serif; font-size: 11px; border-radius: 0px; }
QWidget { background-color: %(bg)s; color: %(text)s;
          selection-background-color: %(sel)s; selection-color: %(hl)s; }
QMainWindow, QDialog { background-color: %(bg)s; }
QFrame#panel { background-color: %(bgsoft)s; border: 2px solid %(dark)s;
               border-top-color: %(bevel)s; border-left-color: %(bevel)s; padding: 6px; }
QFrame#action_card { background-color: %(surface)s; border: 2px solid %(dark)s;
                    border-top-color: %(hl)s; border-left-color: %(hl)s; padding: 8px; }
QLabel#title { color: %(text)s; font-size: 13px; font-weight: bold; }
QLabel#banner { background-color: %(surface)s; color: %(hl)s; border: 2px solid %(dark)s;
                border-top-color: %(bevel)s; border-left-color: %(bevel)s;
                padding: 6px 10px; font-size: 12px; font-weight: bold; }
QLabel#sub { color: %(text2)s; }
QLabel#muted { color: %(muted)s; }
QLabel#danger { color: %(dangertext)s; font-weight: bold; }
QPushButton { background-color: %(raised)s; color: %(text)s; border: 2px solid %(dark)s;
              border-top-color: %(bevel)s; border-left-color: %(bevel)s;
              padding: 4px 10px; min-height: 20px; font-weight: bold; }
QPushButton:hover { background-color: %(alt)s; }
QPushButton:pressed { background-color: %(compare)s; border-top-color: %(dark)s;
                      border-left-color: %(dark)s; border-bottom-color: %(bevel)s;
                      border-right-color: %(bevel)s; color: %(hl)s; }
QPushButton:disabled { color: %(muted)s; background-color: %(surface)s; border-color: %(dark)s; }
QPushButton#selected { background-color: %(compare)s; color: %(hl)s;
                       border-top-color: %(dark)s; border-left-color: %(dark)s;
                       border-bottom-color: %(bevel)s; border-right-color: %(bevel)s; }
QPushButton#primary_action { background-color: %(surface)s; color: %(hl)s;
                            border: 2px solid %(dark)s; border-top-color: %(hl)s;
                            border-left-color: %(hl)s; padding: 6px 14px; font-size: 12px; }
QPushButton#primary_action:hover { background-color: %(raised)s; }
QPushButton#danger { color: %(dangertext)s; }
QTableWidget { background-color: %(compare)s; color: %(text)s; gridline-color: %(bordermuted)s;
               border: 2px solid %(dark)s; border-top-color: %(dark)s;
               border-left-color: %(dark)s; border-bottom-color: %(bevel)s;
               border-right-color: %(bevel)s; }
QHeaderView::section { background-color: %(surface)s; color: %(text2)s;
                       border: 1px solid %(dark)s; padding: 3px; font-weight: bold; }
QTableWidget::item:selected { background-color: %(sel)s; color: %(hl)s; }
QPlainTextEdit, QLineEdit, QListWidget { background-color: %(compare)s; color: %(text)s;
              border: 2px solid %(dark)s; border-bottom-color: %(bevel)s;
              border-right-color: %(bevel)s; padding: 3px; }
QScrollBar:vertical { background: %(surface)s; width: 14px; }
QScrollBar::handle:vertical { background: %(raised)s; border: 1px solid %(dark)s; min-height: 20px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; }
QTabWidget::pane { background-color: %(bgsoft)s; border: 2px solid %(dark)s;
                   border-top-color: %(bevel)s; border-left-color: %(bevel)s; top: -1px; }
QTabBar::tab { background-color: %(surface)s; color: %(text2)s; border: 2px solid %(dark)s;
               border-top-color: %(bevel)s; border-left-color: %(bevel)s;
               padding: 6px 14px; margin-right: 2px; font-weight: bold; }
QTabBar::tab:selected { background-color: %(bgsoft)s; color: %(hl)s;
                        border-bottom-color: %(bgsoft)s; }
QTabBar::tab:hover { background-color: %(raised)s; }
QRadioButton { color: %(text)s; spacing: 6px; font-weight: bold; }
QRadioButton::indicator { width: 13px; height: 13px; background-color: %(compare)s;
                          border: 2px solid %(dark)s; border-top-color: %(dark)s;
                          border-left-color: %(dark)s; border-bottom-color: %(bevel)s;
                          border-right-color: %(bevel)s; }
QRadioButton::indicator:checked { background-color: %(hl)s; }
""" % {"bg": C_BG, "bgsoft": C_BG_SOFT, "surface": C_SURFACE, "raised": C_RAISED,
       "alt": C_ALT, "dark": C_BORDER_DARK, "hl": C_BORDER_HL, "bevel": C_BEVEL_LIGHT,
       "bordermuted": C_BORDER_MUTED, "text": C_TEXT, "text2": C_TEXT2,
       "muted": C_MUTED, "dangertext": C_DANGER_TEXT, "sel": C_SELECTION,
       "compare": C_COMPARE}

STATE_TEXT = {
    sa_state.LOCKED: "LOCKED - vault detached, key required",
    sa_state.AUTH_REQUIRED: "AUTH_REQUIRED - security key needed to continue",
    sa_state.AUTHENTICATING: "AUTHENTICATING - touch your security key",
    sa_state.UNLOCKING: "UNLOCKING - decrypting and attaching the vault",
    sa_state.MOUNTED: "MOUNTED - vault is readable",
    sa_state.RUNNING: "RUNNING - application open, vault readable",
    sa_state.SESSION_CACHED: "SESSION_CACHED - vault detached, session still valid",
    sa_state.LOCKING: "LOCKING - closing the application and detaching",
    sa_state.RECOVERY_REQUIRED: "RECOVERY_REQUIRED - previous session did not relock",
    sa_state.ERROR: "ERROR - see the message below",
}

VAULT_TEXT = {
    sa_state.LOCKED: "DETACHED",
    sa_state.AUTH_REQUIRED: "DETACHED",
    sa_state.AUTHENTICATING: "DETACHED",
    sa_state.UNLOCKING: "ATTACHING",
    sa_state.MOUNTED: "MOUNTED (readable)",
    sa_state.RUNNING: "MOUNTED (readable)",
    sa_state.SESSION_CACHED: "DETACHED",
    sa_state.LOCKING: "DETACHING",
    sa_state.RECOVERY_REQUIRED: "UNKNOWN - may still be mounted",
    sa_state.ERROR: "UNKNOWN",
}

DEFAULT_MODE_EXPLANATION = (
    "Closing Obsidian hides and unmounts the vault.\n"
    "You may reopen without the key during the cached session.\n"
    "Lock now, Windows Lock, or session expiry requires the key again."
)

AGGRESSIVE_MODE_EXPLANATION = (
    "Closing Obsidian fully locks the vault.\n"
    "Every new opening requires YubiKey authentication."
)

# How long a worker waits for a person. After the wait the answer is NO /
# CANCELLED -- the acceptance sequence never assumes an approval and never
# silently walks past a physical instruction.
CONFIRM_RESPONSE_TIMEOUT_SECONDS = 180.0
PAUSE_RESPONSE_TIMEOUT_SECONDS = 600.0
PIN_RESPONSE_TIMEOUT_SECONDS = 120.0

# The visible acceptance table is the acceptance RUNNER's own check list, one
# row per real check. The 12 broad gate flags sa_acceptance records are the
# conjunction of these checks, so a row can never be a gate flag: PASS is
# reported for a check the runner actually reported, never inferred from a
# flag that only becomes True once every sibling passed.
ACCEPTANCE_CHECKS = tuple(
    (check_id, "%s  %s" % (check_id, text))
    for check_id, _flag, text in sa_acceptance_runner.CHECKS
)
ACCEPTANCE_CHECK_ROW = {
    check_id: index for index, (check_id, _label) in enumerate(ACCEPTANCE_CHECKS)
}
ACCEPTANCE_CHECK_IDS = tuple(check_id for check_id, _label in ACCEPTANCE_CHECKS)

#: Worst-outcome-wins, so a row that failed once can never be repainted PASS.
ACCEPTANCE_OUTCOME_RANK = {"PASS": 0, "SKIP": 1, "FAIL": 2}
ACCEPTANCE_IDLE = "PENDING"
ACCEPTANCE_RUNNING = "RUNNING"

# A check id the runner reports but the table does not know is a programming
# error, so the two lists are proven to describe the same checks.
assert set(ACCEPTANCE_CHECK_ROW) == set(sa_acceptance_runner.CHECK_FLAGS)


# The setup buttons, each bound to the ONE next action that authorises it.
# The action card is the state machine's public face; these are its hands, and
# they are gated from the same computation, never clicked independently.
SETUP_BUTTON_ACTIONS = (
    ("btn_prep_uac", sa_setup.ACTION_ENABLE_PRIVILEGE_SEPARATION),
    ("btn_install_task", sa_setup.ACTION_INSTALL_HELPER),
    ("btn_test_helper", sa_setup.ACTION_COMMISSION_HELPER),
    ("btn_probe_key", sa_setup.ACTION_PROBE_FIDO2),
    ("btn_run_acceptance", sa_setup.ACTION_RUN_ACCEPTANCE),
    ("btn_import_obsidian", sa_setup.ACTION_IMPORT_APPLICATION),
    ("btn_enroll_vault", sa_setup.ACTION_ENROLL_PROFILE),
    ("btn_migrate_vault", sa_setup.ACTION_MIGRATE_VAULT),
)


def human_seconds(value):
    if value is None:
        return "-"
    value = int(value)
    if value < 60:
        return "%ds" % value
    if value < 3600:
        return "%dm %02ds" % (value // 60, value % 60)
    return "%dh %02dm" % (value // 3600, (value % 3600) // 60)


# ══════════════════════════════════════════════════════════ worker


class AcceptanceWorker(QThread):
    """Runs the disposable acceptance sequence off the window's thread.

    Every question the sequence asks a person is answered by the window's
    thread and handed back through a one-slot queue. The worker blocks while
    it waits, and an answer that never arrives is treated as a refusal.
    """

    checkDone = pyqtSignal(str, bool, str)
    checkOutcome = pyqtSignal(str, str, str)
    logMessage = pyqtSignal(str)
    pinRequested = pyqtSignal(object, str)
    pauseRequested = pyqtSignal(object, str)
    confirmRequested = pyqtSignal(object, str)
    acceptanceFinished = pyqtSignal(int)

    def __init__(self, registry_path, managed_root=None, profile_id="obsidian", parent=None):
        super().__init__(parent)
        self.registry_path = registry_path
        self.managed_root = managed_root
        self.profile_id = profile_id
        self.confirm_timeout = CONFIRM_RESPONSE_TIMEOUT_SECONDS
        self.pause_timeout = PAUSE_RESPONSE_TIMEOUT_SECONDS
        self.pin_timeout = PIN_RESPONSE_TIMEOUT_SECONDS
        self._cancelled = threading.Event()
        self._waiting_lock = threading.Lock()
        self._waiting = None

    # ── cancellation (SRC-027 CORE-001) ──
    def cancel(self):
        """Stop at the next operator round trip; wake the one waiting now.

        Cooperative: the runner's own abort path unwinds the disposable
        container. A cancelled question is a refusal, a cancelled physical
        step aborts, a cancelled PIN is none -- never an approval.
        """
        self._cancelled.set()
        with self._waiting_lock:
            waiting = self._waiting
        if waiting is not None:
            try:
                waiting.put_nowait(None)
            except queue.Full:
                pass

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def _round_trip(self, signal, prompt_text, timeout):
        if self._cancelled.is_set():
            return None
        q = queue.Queue(maxsize=1)
        with self._waiting_lock:
            self._waiting = q
        try:
            signal.emit(q, str(prompt_text))
            try:
                answer = q.get(timeout=timeout)
            except queue.Empty:
                return None
            return None if self._cancelled.is_set() else answer
        finally:
            with self._waiting_lock:
                self._waiting = None

    # ── operator round trips (worker thread -> window thread -> queue) ──
    def _request_pin(self, prompt_text):
        return self._round_trip(self.pinRequested, prompt_text, self.pin_timeout)

    def _request_confirmation(self, prompt_text):
        """A security question. Unanswered, timed out or refused is False."""
        return self._round_trip(self.confirmRequested, prompt_text,
                                self.confirm_timeout) is True

    def _request_pause(self, prompt_text):
        """A physical instruction. Only an explicit Continue resumes.

        Cancel or a timeout raises :class:`sa_acceptance_runner.AbortAcceptance`,
        which the runner turns into an explicit incomplete record -- a paused
        step never resolves itself and is never skipped over silently.
        """
        answer = self._round_trip(self.pauseRequested, prompt_text,
                                  self.pause_timeout)
        if answer is not True:
            raise sa_acceptance_runner.AbortAcceptance(
                "the operator did not complete the physical step: %s" % prompt_text)
        return True

    def callbacks(self):
        """The runner's callbacks, as this worker binds them.

        Named and returned rather than closed over inside :meth:`run` so a
        regression can drive a single callback without a key, a container or
        a person -- the confirm path in particular is a security property.
        """
        def echo_cb(msg):
            self.logMessage.emit(str(msg))

        def check_cb(check_id, ok, detail):
            self.checkDone.emit(str(check_id), bool(ok), str(detail))

        def outcome_cb(check_id, outcome, detail):
            self.checkOutcome.emit(str(check_id), str(outcome), str(detail))

        return {
            "echo": echo_cb,
            "check": check_cb,
            "outcome": outcome_cb,
            "pin": self._request_pin,
            "confirm": self._request_confirmation,
            "pause": self._request_pause,
        }

    def run(self):
        import argparse
        import sa_acceptance_runner

        args = argparse.Namespace(
            registry=self.registry_path,
            managed_root=self.managed_root,
            profile=self.profile_id,
            size_gb=1,
            mount_parent=None,
            evidence=None,
            additional=False,
            disposable_acceptance=True,
        )

        callbacks = self.callbacks()
        runner = sa_acceptance_runner.DisposableAcceptance(
            args,
            echo=callbacks["echo"],
            confirm_callback=callbacks["confirm"],
            pause_callback=callbacks["pause"],
            pin_callback=callbacks["pin"],
            check_callback=callbacks["check"],
            outcome_callback=callbacks["outcome"],
        )
        try:
            code = runner.execute()
        except Exception as exc:
            self.logMessage.emit("Acceptance runner failed: %s" % exc)
            code = 1
        self.acceptanceFinished.emit(code)

class BrokerWorker(QObject):
    statusReady = pyqtSignal(list)
    tickCompleted = pyqtSignal()
    operationDone = pyqtSignal(dict)
    auditLine = pyqtSignal(str)
    fatal = pyqtSignal(str)
    recoveryMaterial = pyqtSignal(str, str)
    recoveryAnswer = pyqtSignal(bool)
    enrollDone = pyqtSignal(dict)
    capabilitiesReady = pyqtSignal(dict)
    keysReady = pyqtSignal(str, list)
    # Two distinct facts with two distinct consumers: a MEASURED preflight
    # report (cached, drives setup state) and the RESULT of one privileged
    # mutation (shown to the person, never cached as a report).
    privilegedStatusReady = pyqtSignal(dict)
    privilegedActionResult = pyqtSignal(dict)
    pinRequested = pyqtSignal(object)
    shutdownComplete = pyqtSignal(dict)
    acceptanceCheckDone = pyqtSignal(str, str)   # (check_name, status)
    acceptanceFinished = pyqtSignal(dict)

    def __init__(self, registry_path, managed_root=None,
                 recovery_ack_timeout=sa_enroll.RECOVERY_ACK_TIMEOUT_SECONDS):
        QObject.__init__(self)
        self.registry_path = registry_path
        self.managed_root = managed_root
        self.broker = None
        self._pending = None
        #: A resolved enrollment whose storage cleanup is still owed
        #: (ENROLL_CLEANUP_REQUIRED). Part of the shutdown barrier.
        self._cleanup_pending = None
        self._recovery_ack_timeout = recovery_ack_timeout
        self.window_handle = None

    def _request_pin(self, rp_id=None):
        q = queue.Queue(maxsize=1)
        self.pinRequested.emit(q)
        try:
            return q.get(timeout=60)
        except queue.Empty:
            return None

    @pyqtSlot()
    def bootstrap(self):
        try:
            registry = sa_config.load_registry(self.registry_path,
                                               managed_root=self.managed_root)
            os.makedirs(registry.state_dir, exist_ok=True)
            audit = sa_audit.AuditLog(registry.audit_path,
                                      echo=lambda line: self.auditLine.emit(line))
            self.broker = sa_broker.SecureBroker(
                registry, audit=audit, window_handle=self.window_handle,
                pin_callback=self._request_pin)
            self.broker.reconcile_all()
            self.push_status()
        except Exception as exc:
            self.fatal.emit("%s: %s" % (type(exc).__name__, exc))

    @pyqtSlot()
    def tick(self):
        try:
            self._expire_pending_enrollment()
            if self.broker is None:
                return
            snaps = {pid: rt.supervisor.observation()
                     for pid, rt in self.broker._runtimes.items()}
            self.broker.poll_foreground(snapshots=snaps)
            for event in self.broker.tick(snapshots=snaps):
                if event is not None:
                    self.operationDone.emit(event.to_dict())
            # One snapshot per profile per turn is consumed here; do NOT
            # forward snaps into status_all unless the tick just built them.
            self.push_status(snapshots=snaps)
        except Exception as exc:
            self.auditLine.emit("tick failed: %s" % type(exc).__name__)
        finally:
            self.tickCompleted.emit()

    def push_status(self, snapshots=None):
        if self.broker is None:
            return
        self.statusReady.emit(self.broker.status_all(snapshots=snapshots))

    @pyqtSlot(str)
    def openProfile(self, profile_id):
        if self.broker is None:
            return
        try:
            result = self.broker.open(profile_id)
            self.operationDone.emit(result.to_dict())
        except Exception as exc:
            self.operationDone.emit({"ok": False, "profile_id": profile_id,
                                     "reason": "%s: %s" % (type(exc).__name__, exc),
                                     "error_category": "internal"})
        self.push_status()

    @pyqtSlot()
    def privilegedStatus(self):
        """Measure the privileged picture. Heavyweight: worker thread only."""
        try:
            script_dir = ntpath.dirname(ntpath.abspath(__file__))
            report = sa_privtask.preflight(script_dir=script_dir)
            self.privilegedStatusReady.emit(report)
        except Exception as exc:
            err = {"ok": False, "reasons": ["%s: %s" % (type(exc).__name__, exc)]}
            self.privilegedStatusReady.emit(err)

    @pyqtSlot(str, int)
    def privilegedAction(self, action, idle_timeout):
        import sa_cli
        payload = {"action": action}
        try:
            script_dir = ntpath.dirname(ntpath.abspath(__file__))
            if action in ("check", "check_start"):
                payload.update(sa_cli.run_privileged_setup(
                    "check", idle_timeout=idle_timeout or None,
                    script_dir=script_dir, confirm=True))
                if action == "check_start" or payload.get("ok"):
                    payload["commission"] = sa_privtask.commission(script_dir=script_dir)
                    payload["ok"] = bool(payload["commission"]["ok"])
            else:
                payload.update(sa_cli.run_privileged_setup(
                    action, idle_timeout=idle_timeout or None,
                    script_dir=script_dir, confirm=True))
        except Exception as exc:
            payload["ok"] = False
            payload["message"] = "%s: %s" % (type(exc).__name__, exc)
        self.privilegedActionResult.emit(payload)
        # A mutation invalidates the cached measurement: request a fresh one
        # rather than letting an install/repair/remove payload stand in for it.
        self.privilegedStatus()
        self.push_status()

    @pyqtSlot(str)
    def lockProfile(self, profile_id):
        if self.broker is None:
            return
        try:
            for result in self.broker.lock_now(profile_id or None):
                self.operationDone.emit(result.to_dict())
        except Exception as exc:
            self.operationDone.emit({"ok": False, "profile_id": profile_id,
                                     "reason": str(exc), "error_category": "internal"})
        self.push_status()

    @pyqtSlot(str)
    def recoverProfile(self, profile_id):
        if self.broker is None:
            return
        try:
            self.broker.reconcile(profile_id)
            self.operationDone.emit(self.broker.recover(profile_id).to_dict())
        except Exception as exc:
            self.operationDone.emit({"ok": False, "profile_id": profile_id,
                                     "reason": str(exc), "error_category": "internal"})
        self.push_status()

    @pyqtSlot(str, str)
    def setMode(self, profile_id, mode):
        if self.broker is None:
            return
        try:
            self.operationDone.emit(self.broker.set_mode(profile_id, mode).to_dict())
        except Exception as exc:
            self.operationDone.emit({"ok": False, "profile_id": profile_id,
                                     "reason": str(exc), "error_category": "internal"})
        self.push_status()

    @pyqtSlot(str)
    def noteInteraction(self, profile_id):
        if self.broker is not None:
            self.broker.note_activity(profile_id)
            self.push_status()

    @pyqtSlot(str)
    def systemEvent(self, event):
        if self.broker is None:
            return
        try:
            for result in self.broker.on_system_event(event):
                self.operationDone.emit(result.to_dict())
        except Exception as exc:
            self.auditLine.emit("system event %s failed: %s" % (event, type(exc).__name__))
        self.push_status()

    @pyqtSlot(int)
    def setWindowHandle(self, handle):
        self.window_handle = int(handle) or None
        if self.broker is not None:
            self.broker.window_handle = self.window_handle

    @pyqtSlot(str)
    def probeCapabilities(self, profile_id):
        if self.broker is None:
            return
        try:
            caps = sa_enroll.capabilities(self.broker, profile_id)
        except Exception as exc:
            caps = {"provider": "?", "available": False, "hmac_secret": False,
                    "authenticators": 0, "detail": str(exc),
                    "transport": "UNAVAILABLE", "user_verification": False,
                    "library_version": ""}
        caps["profile_id"] = profile_id
        self.capabilitiesReady.emit(caps)

    @pyqtSlot(str)
    def listKeys(self, profile_id):
        if self.broker is None:
            return
        try:
            self.keysReady.emit(profile_id, sa_enroll.enrolled_keys(self.broker, profile_id))
        except Exception:
            self.keysReady.emit(profile_id, [])

    @pyqtSlot(bool)
    def acknowledgeRecovery(self, accepted):
        pending, self._pending = self._pending, None
        if pending is None:
            return
        profile_id = pending.profile_id
        try:
            result = pending.accept() if accepted else pending.decline()
            self._keep_if_cleanup_owed(pending)
            self.enrollDone.emit(result.to_dict())
        except Exception as exc:
            self.enrollDone.emit({"ok": False, "profile_id": profile_id,
                                  "reason": str(exc),
                                  "state": sa_enroll.ENROLL_FAILED,
                                  "error_category": getattr(exc, "category", "auth_failed")})
        self.push_status()

    @pyqtSlot(str, bool)
    def enroll(self, profile_id, additional):
        if self.broker is None:
            return
        try:
            if additional:
                result = sa_enroll.enroll_additional(self.broker, profile_id)
                self.enrollDone.emit(result.to_dict())
            else:
                if self._pending is not None:
                    raise sa_enroll.EnrollmentRefused(
                        "an enrollment for %r is already waiting for its "
                        "recovery acknowledgement" % self._pending.profile_id,
                        "concurrent_request")
                pending, recovery = sa_enroll.begin_enrollment(
                    self.broker, profile_id,
                    timeout_seconds=self._recovery_ack_timeout)
                self._pending = pending
                self.recoveryMaterial.emit(profile_id, recovery)
                recovery = None
        except Exception as exc:
            self.enrollDone.emit({"ok": False, "profile_id": profile_id,
                                  "reason": str(exc),
                                  "state": sa_enroll.ENROLL_FAILED,
                                  "error_category": getattr(exc, "category", "auth_failed")})
        self.push_status()

    @pyqtSlot(str, str)
    def importApp(self, profile_id, source):
        if self.broker is None:
            return
        try:
            result = sa_enroll.import_application(self.broker, profile_id, source, overwrite=True)
            self.operationDone.emit({"ok": True, "profile_id": profile_id, "action": "import_app",
                                     "reason": "Obsidian imported into managed storage: %s" % result.get("destination")})
        except Exception as exc:
            self.operationDone.emit({"ok": False, "profile_id": profile_id, "action": "import_app",
                                     "reason": str(exc), "error_category": getattr(exc, "category", "internal")})
        self.push_status()

    @pyqtSlot(str, str)
    def removeKey(self, profile_id, credential_ident):
        if self.broker is None:
            return
        try:
            result = sa_enroll.remove_credential_authenticated(self.broker, profile_id, credential_ident)
            self.operationDone.emit(result.to_dict())
            self.keysReady.emit(profile_id, sa_enroll.enrolled_keys(self.broker, profile_id))
        except Exception as exc:
            self.operationDone.emit({"ok": False, "profile_id": profile_id, "action": "remove_key",
                                     "reason": str(exc), "error_category": getattr(exc, "category", "auth_failed")})
        self.push_status()

    @pyqtSlot(str, str)
    def migrateVault(self, profile_id, source):
        if self.broker is None:
            return
        try:
            report = sa_migrate.migrate(self.broker, profile_id, source)
            self.operationDone.emit({
                "ok": report.ok,
                "profile_id": profile_id,
                "action": "migrate",
                "reason": report.reason or ("Vault migrated successfully" if report.ok else "Migration failed"),
                "result": report.to_dict(),
            })
        except Exception as exc:
            self.operationDone.emit({
                "ok": False,
                "profile_id": profile_id,
                "action": "migrate",
                "reason": str(exc),
                "error_category": getattr(exc, "category", "internal"),
            })
        self.push_status()

    @pyqtSlot(str)
    def migrateRecover(self, profile_id):
        if self.broker is None:
            return
        try:
            result = sa_migrate.recover(self.broker, profile_id)
            self.operationDone.emit({"ok": True, "profile_id": profile_id, "action": "migrate_recover",
                                     "reason": "Migration recovered: %s" % result})
        except Exception as exc:
            self.operationDone.emit({"ok": False, "profile_id": profile_id, "action": "migrate_recover",
                                     "reason": str(exc), "error_category": getattr(exc, "category", "internal")})
        self.push_status()

    def _expire_pending_enrollment(self):
        pending = self._pending
        if pending is None or not pending.expired:
            return
        self._pending = None
        profile_id = pending.profile_id
        try:
            result = pending.decline(timed_out=True)
            self._keep_if_cleanup_owed(pending)
            self.enrollDone.emit(result.to_dict())
        except Exception as exc:
            self.enrollDone.emit({"ok": False, "profile_id": profile_id,
                                  "reason": str(exc),
                                  "state": sa_enroll.ENROLL_FAILED,
                                  "error_category": "internal"})

    def _keep_if_cleanup_owed(self, pending):
        if getattr(pending, "cleanup_required", False):
            self._cleanup_pending = pending

    def _cancel_pending_enrollment(self, reason):
        """Cancel an unacknowledged enrollment; return its authoritative result.

        A cancellation whose container could not be removed is not dropped:
        it stays owed in ``_cleanup_pending`` (SRC-027 W2-002).
        """
        pending, self._pending = self._pending, None
        if pending is None:
            return None
        try:
            result = pending.cancel(reason)
        except Exception as exc:
            result = sa_enroll.EnrollmentResult(
                False, pending.profile_id, state=sa_enroll.ENROLL_FAILED,
                reason="%s: %s" % (type(exc).__name__, exc),
                error_category="internal")
        self._keep_if_cleanup_owed(pending)
        try:
            self.enrollDone.emit(result.to_dict())
        except Exception:
            pass
        return result

    def _settle_enrollment_cleanup(self):
        """Retry owed enrollment cleanup. Returns the unresolved profile id or None."""
        pending = self._cleanup_pending
        if pending is None:
            return None
        try:
            result = pending.retry_cleanup()
            self.enrollDone.emit(result.to_dict())
        except Exception:
            pass
        if getattr(pending, "cleanup_required", False):
            return pending.profile_id
        self._cleanup_pending = None
        return None

    @pyqtSlot()
    def retryEnrollmentCleanup(self):
        self._settle_enrollment_cleanup()
        self.push_status()

    @pyqtSlot()
    def shutdown(self):
        self._cancel_pending_enrollment(
            "Secure Apps closed before the recovery material was "
            "acknowledged; the empty container was removed")
        payload = {"ok": True, "recovery_required": [], "reason": "", "profiles": []}
        owed = self._settle_enrollment_cleanup()
        if owed is not None:
            payload["ok"] = False
            payload["recovery_required"].append(owed)
            payload["reason"] = (
                "enrollment storage cleanup for %s is unresolved (staging "
                "volume attached or container not removed); retry before "
                "closing" % owed)
        if self.broker is None:
            self.shutdownComplete.emit(payload)
            return
        try:
            for result in self.broker.shutdown() or []:
                if result is None:
                    continue
                entry = result.to_dict()
                payload["profiles"].append(entry)
                if (not entry.get("ok")
                        or entry.get("state") == sa_state.RECOVERY_REQUIRED):
                    payload["ok"] = False
                    payload["recovery_required"].append(entry.get("profile_id"))
        except Exception as exc:
            payload["ok"] = False
            payload["reason"] = "%s: %s" % (type(exc).__name__, exc)
        if not payload["ok"] and not payload["reason"]:
            payload["reason"] = (
                "one or more protected volumes did not detach cleanly: %s"
                % ", ".join(str(p) for p in payload["recovery_required"]))
        try:
            self.push_status()
        except Exception:
            pass
        self.shutdownComplete.emit(payload)


# ══════════════════════════════════════════════════════════ dialogs
class RecoveryDialog(QDialog):
    """Shows the BitLocker recovery material once and demands typed confirmation."""

    CONFIRM = "I HAVE STORED IT"

    def __init__(self, profile_id, recovery, parent=None):
        QDialog.__init__(self, parent)
        self.recovery_text = recovery
        self.setWindowTitle("Secure Apps - BitLocker Recovery Material")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)

        head = QLabel("BitLocker recovery material for '%s'" % profile_id)
        head.setObjectName("title")
        layout.addWidget(head)

        warn = QLabel(
            "Save this BitLocker recovery password offline.\n\n"
            "It can recover your data if every enrolled YubiKey is lost.\n\n"
            "SAITULS does not store this password.")
        warn.setObjectName("danger")
        layout.addWidget(warn)

        self.text = QPlainTextEdit(recovery)
        self.text.setReadOnly(True)
        self.text.setFixedHeight(80)
        layout.addWidget(self.text)

        btn_row = QHBoxLayout()
        btn_copy = QPushButton("Copy")
        btn_print = QPushButton("Print / Save instructions")
        btn_row.addWidget(btn_copy)
        btn_row.addWidget(btn_print)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        btn_copy.clicked.connect(self._copy)
        btn_print.clicked.connect(self._instructions)

        layout.addWidget(QLabel("Type  %s  to continue (anything else aborts):" % self.CONFIRM))
        self.entry = QLineEdit()
        layout.addWidget(self.entry)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)
        self.entry.textChanged.connect(
            lambda t: self._ok.setEnabled(t.strip().upper() == self.CONFIRM))

    def _copy(self):
        QApplication.clipboard().setText(self.recovery_text)
        QMessageBox.information(self, "Copied", "BitLocker recovery password copied to clipboard.")

    def _instructions(self):
        QMessageBox.information(
            self, "Recovery Instructions",
            "HOW TO STORE YOUR RECOVERY PASSWORD SAFELY:\n\n"
            "1. Write down the 48-digit key on paper and store it in a physical safe.\n"
            "2. Or store it in an offline password manager (KeePass, 1Password, Bitwarden).\n"
            "3. DO NOT save it as a plaintext file on this machine.\n"
            "4. DO NOT take a screenshot or send it over network.\n\n"
            "If all YubiKeys are lost, you can use Windows Disk Management and BitLocker "
            "to unlock the VHDX container with this key.")

    def _accept(self):
        if self.entry.text().strip().upper() == self.CONFIRM:
            self.accept()


class PinDialog(QDialog):
    """Masked PIN entry for FIDO2 operations requiring user verification."""

    def __init__(self, parent=None, prompt="Enter FIDO2 Security Key PIN:"):
        QDialog.__init__(self, parent)
        self.setWindowTitle("Secure Apps - Security Key PIN")
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        head = QLabel(prompt)
        head.setObjectName("title")
        layout.addWidget(head)
        sub = QLabel("User verification is required to access this security key.")
        sub.setObjectName("sub")
        layout.addWidget(sub)
        self.entry = QLineEdit()
        self.entry.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.entry)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)
        self.entry.textChanged.connect(lambda t: self._ok.setEnabled(len(t) >= 4))
        self.entry.setFocus()

    def get_pin(self):
        val = self.entry.text()
        self.entry.clear()
        return val


class PrivilegedHelperDialog(QDialog):
    """Detailed UAC diagnostics and privileged helper inspection."""

    ROWS = (
        ("EnableLUA", "EnableLUA (privilege separation)"),
        ("ConsentPromptBehaviorAdmin", "Admin consent prompt behavior"),
        ("PromptOnSecureDesktop", "Prompt on secure desktop"),
        ("broker_integrity", "Secure Apps broker integrity"),
        ("user_is_administrator", "Account has Administrator membership"),
        ("admin_approval_mode", "Admin Approval Mode active"),
        ("scheduled_helper_installed", "Privileged task installed"),
        ("scheduled_helper_definition_valid", "Task definition valid"),
        ("scheduled_helper_runnable", "Task runnable"),
        ("scheduled_task_state", "Task state"),
        ("task_path", "Task name"),
        ("launch_mode", "Launch mode"),
        ("idle_timeout_seconds", "Helper idle timeout (s)"),
        ("helper_integrity", "Privileged helper"),
        ("pipe_authentication", "Pipe authentication"),
        ("pin_writable_by_user", "Pin writable by a normal user"),
    )

    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle("Secure Apps - Privileged Helper Details")
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)
        title = QLabel("PRIVILEGED HELPER & UAC DIAGNOSTICS")
        title.setObjectName("title")
        layout.addWidget(title)

        panel = QFrame()
        panel.setObjectName("panel")
        grid = QGridLayout(panel)
        grid.setVerticalSpacing(3)
        self.values = {}
        for index, (key, caption) in enumerate(self.ROWS):
            label = QLabel(caption + ":")
            label.setObjectName("sub")
            value = QLabel("-")
            value.setWordWrap(True)
            grid.addWidget(label, index, 0)
            grid.addWidget(value, index, 1)
            self.values[key] = value
        layout.addWidget(panel)

        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def apply_report(self, report):
        report = report or {}
        for key, _caption in self.ROWS:
            value = report.get(key)
            if value is True:
                text = "YES"
            elif value is False:
                text = "NO"
            elif value in (None, ""):
                text = "-"
            else:
                text = str(value)
            self.values[key].setText(text)
        reasons = report.get("reasons") or []
        commission = report.get("commission") or {}
        lines = []
        if report.get("message"):
            lines.append(str(report["message"]))
        if commission:
            lines.append("silent start: %s; helper high integrity: %s; broker medium integrity: %s"
                         % (commission.get("silent_start"),
                            commission.get("helper_high_integrity"),
                            commission.get("broker_medium_integrity")))
            reasons = list(reasons) + list(commission.get("reasons") or [])
        if report.get("token"):
            lines.append(str(report["token"]))
        lines.extend(str(reason) for reason in reasons)
        self.message.setText("\n".join(lines) if lines else "No problems reported.")
        self.message.setStyleSheet("color: %s;" % (C_DANGER_TEXT if reasons else C_TEXT2))


class AcceptanceConfirmDialog(QDialog):
    """A real answer to a security-relevant acceptance question.

    Nothing on this dialog approves by itself: the default button is the
    refusing one, so an accidental Enter refuses. Closing the window refuses.
    """

    def __init__(self, prompt_text, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle("Secure Apps - Operator Confirmation Required")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        head = QLabel("OPERATOR CONFIRMATION REQUIRED")
        head.setObjectName("title")
        layout.addWidget(head)

        note = QLabel(
            "Secure Apps does not answer this for you. If you are unsure, refuse --\n"
            "an unanswered or refused question never becomes an approval.")
        note.setObjectName("danger")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.prompt = QLabel(str(prompt_text))
        self.prompt.setWordWrap(True)
        layout.addWidget(self.prompt)

        row = QHBoxLayout()
        self.btn_refuse = QPushButton("DO NOT APPROVE")
        self.btn_refuse.setObjectName("primary_action")
        self.btn_approve = QPushButton("APPROVE")
        row.addWidget(self.btn_refuse)
        row.addWidget(self.btn_approve)
        row.addStretch()
        layout.addLayout(row)

        self.btn_approve.clicked.connect(self.accept)
        self.btn_refuse.clicked.connect(self.reject)
        # Refuse is the default: Enter refuses rather than approves.
        self.btn_refuse.setDefault(True)
        self.btn_refuse.setFocus()

    def approved(self, code):
        return code == QDialog.DialogCode.Accepted


class PhysicalStepDialog(QDialog):
    """A physical instruction the operator must finish before the run resumes.

    No timer closes this dialog: the worker is blocked and stays blocked. The
    answer is only ever an explicit Continue or an explicit Cancel, and Cancel
    stops the acceptance sequence with an incomplete record.
    """

    def __init__(self, prompt_text, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle("Secure Apps - Physical Step Required")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)

        head = QLabel("SECURE APPS IS WAITING FOR YOU")
        head.setObjectName("title")
        layout.addWidget(head)

        note = QLabel(
            "The test has stopped at a physical step and will not continue on its\n"
            "own. Nothing is written to your real vault by this test.")
        note.setObjectName("sub")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.prompt = QLabel(str(prompt_text))
        self.prompt.setWordWrap(True)
        layout.addWidget(self.prompt)

        row = QHBoxLayout()
        self.btn_continue = QPushButton("Continue")
        self.btn_continue.setObjectName("primary_action")
        self.btn_cancel = QPushButton("Cancel acceptance")
        self.btn_cancel.setObjectName("danger")
        row.addWidget(self.btn_continue)
        row.addWidget(self.btn_cancel)
        row.addStretch()
        layout.addLayout(row)

        self.btn_continue.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_continue.setDefault(True)
        self.btn_continue.setFocus()


class SessionEventFilter(QAbstractNativeEventFilter):
    WM_WTSSESSION_CHANGE = 0x02B1
    WTS_SESSION_LOCK = 0x7
    WTS_SESSION_LOGOFF = 0x6
    WM_POWERBROADCAST = 0x0218
    PBT_APMSUSPEND = 0x0004
    WM_QUERYENDSESSION = 0x0011
    WM_ENDSESSION = 0x0016
    NOTIFY_FOR_THIS_SESSION = 0

    def __init__(self, emit_fn):
        QAbstractNativeEventFilter.__init__(self)
        self.emit_event = emit_fn
        self._registered = False
        self._hwnd = None

    def register(self, hwnd):
        if os.name != "nt" or not hwnd:
            return
        self._hwnd = hwnd
        try:
            import ctypes
            wtsapi32 = ctypes.windll.wtsapi32
            self._registered = bool(wtsapi32.WTSRegisterSessionNotification(
                hwnd, self.NOTIFY_FOR_THIS_SESSION))
        except Exception:
            self._registered = False

    def unregister(self):
        if self._registered and self._hwnd:
            try:
                import ctypes
                ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(self._hwnd)
            except Exception:
                pass
        self._registered = False

    def nativeEventFilter(self, eventType, message):
        if os.name != "nt":
            return False, 0
        try:
            import ctypes
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(message.__int__())
            if msg.message == self.WM_WTSSESSION_CHANGE:
                if msg.wParam == self.WTS_SESSION_LOCK:
                    self.emit_event(sa_broker.EVENT_WORKSTATION_LOCK)
                elif msg.wParam == self.WTS_SESSION_LOGOFF:
                    self.emit_event(sa_broker.EVENT_LOGOFF)
            elif msg.message == self.WM_POWERBROADCAST:
                if msg.wParam == self.PBT_APMSUSPEND:
                    self.emit_event(sa_broker.EVENT_SUSPEND)
            elif msg.message in (self.WM_QUERYENDSESSION, self.WM_ENDSESSION):
                self.emit_event(sa_broker.EVENT_SHUTDOWN)
        except Exception:
            pass
        return False, 0


# ══════════════════════════════════════════════════════════ main window
class SecureAppsWindow(QMainWindow):
    requestOpen = pyqtSignal(str)
    requestLock = pyqtSignal(str)
    requestRecover = pyqtSignal(str)
    requestMode = pyqtSignal(str, str)
    requestTick = pyqtSignal()
    requestBootstrap = pyqtSignal()
    requestInteraction = pyqtSignal(str)
    requestSystemEvent = pyqtSignal(str)
    requestCapabilities = pyqtSignal(str)
    requestKeys = pyqtSignal(str)
    requestEnroll = pyqtSignal(str, bool)
    requestAck = pyqtSignal(bool)
    requestShutdown = pyqtSignal()
    requestWindowHandle = pyqtSignal(int)
    requestPrivilegedStatus = pyqtSignal()
    requestPrivilegedAction = pyqtSignal(str, int)
    requestImportApp = pyqtSignal(str, str)
    requestRemoveKey = pyqtSignal(str, str)
    requestMigrate = pyqtSignal(str, str)
    requestMigrateRecover = pyqtSignal(str)

    SHUTDOWN_IDLE = "idle"
    SHUTDOWN_RUNNING = "running"
    SHUTDOWN_DONE = "done"
    SHUTDOWN_FAILED = "failed"
    SHUTDOWN_TIMEOUT_MS = 180000
    THREAD_JOIN_MS = 15000

    COLUMNS = ("Profile", "State", "Vault", "Session", "Last activity", "Idle timeout")

    #: When a broker operation fails for one of these reasons the cached
    #: privileged measurement is no longer trustworthy (an installed helper was
    #: tampered with, the boundary changed, UAC moved), so it is re-measured.
    PRIVILEGED_REFRESH_TOKENS = ("tamper", "privileged", "enablelua", "integrity", "uac")

    def __init__(self, registry_path=None, managed_root=None, initial_profile=None):
        QMainWindow.__init__(self)
        self.registry_path = registry_path or sa_config.default_registry_path()
        self.managed_root = managed_root
        self.setWindowTitle("SAITULS - Secure Apps")
        self.resize(840, 680)
        self._rows = []
        self._selected = initial_profile or "obsidian"
        self._last_probe = None
        self._last_preflight = None
        self._last_acceptance = None
        # PERF-002: reproducible setup-security facts are cached here and only
        # recomputed when an invalidation event lands; runtime status ticks
        # never touch it.
        self._setup_snapshot = None
        self._setup_snapshot_dirty = True
        self._last_keys = []
        # None means UNRESOLVED, not False. The durable reboot marker is the
        # only authority; the window never talks itself out of a pending
        # restart, and it never invents one either.
        self._reboot_pending = None
        self._commissioned = False
        self._acceptance_process = None
        self._acc_worker = None
        self._acceptance_running = False
        self._acceptance_cursor_id = None
        #: check_id -> worst outcome reported for it in this window's run.
        self._acceptance_terminal = {}
        self._current_next_action = None

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # ── Top Status Banner ───────────────────────────────────────────
        self.banner = QLabel("SECURE APPS: INITIALIZING...")
        self.banner.setObjectName("banner")
        outer.addWidget(self.banner)

        # ── Three Primary Tabs ──────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tab_apps = QWidget()
        self.tab_setup = QWidget()
        self.tab_recovery = QWidget()

        self.tabs.addTab(self.tab_apps, "Apps")
        self.tabs.addTab(self.tab_setup, "Setup & Security")
        self.tabs.addTab(self.tab_recovery, "Recovery")
        outer.addWidget(self.tabs, stretch=1)

        self._build_apps_tab()
        self._build_setup_tab()
        self._build_recovery_tab()

        # ── Bottom Log & Global Tools ───────────────────────────────────
        log_panel = QFrame()
        log_panel.setObjectName("panel")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(4, 4, 4, 4)
        log_layout.setSpacing(4)

        log_header_row = QHBoxLayout()
        log_title = QLabel("SECURE APPS EVENT LOG")
        log_title.setObjectName("sub")
        log_header_row.addWidget(log_title)
        log_header_row.addStretch()

        self.btn_copy_diag = QPushButton("Copy diagnostics")
        self.btn_refresh = QPushButton("Refresh")
        log_header_row.addWidget(self.btn_copy_diag)
        log_header_row.addWidget(self.btn_refresh)
        log_layout.addLayout(log_header_row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setFixedHeight(95)
        log_layout.addWidget(self.log)
        outer.addWidget(log_panel)

        # ── Signals Wiring ──────────────────────────────────────────────
        self.btn_copy_diag.clicked.connect(self._copy_diagnostics)
        self.btn_refresh.clicked.connect(self._refresh_all)

        self.thread = QThread(self)
        self.worker = BrokerWorker(self.registry_path, managed_root)
        self.worker.moveToThread(self.thread)
        self.worker.statusReady.connect(self._apply_status)
        self.worker.tickCompleted.connect(self._tick_acknowledged)
        self.worker.operationDone.connect(self._apply_operation)
        self.worker.auditLine.connect(self._append_log)
        self.worker.fatal.connect(self._fatal)
        self.worker.recoveryMaterial.connect(self._show_recovery)
        self.worker.pinRequested.connect(self._prompt_pin)
        self.worker.enrollDone.connect(self._enroll_done)
        self.worker.capabilitiesReady.connect(self._caps_ready)
        self.worker.keysReady.connect(self._keys_ready)
        self.worker.privilegedStatusReady.connect(self._privileged_status_ready)
        self.worker.privilegedActionResult.connect(self._privileged_action_result)
        self.worker.shutdownComplete.connect(self._shutdown_done)

        self.requestBootstrap.connect(self.worker.bootstrap)
        self.requestTick.connect(self.worker.tick)
        self.requestOpen.connect(self.worker.openProfile)
        self.requestLock.connect(self.worker.lockProfile)
        self.requestRecover.connect(self.worker.recoverProfile)
        self.requestMode.connect(self.worker.setMode)
        self.requestInteraction.connect(self.worker.noteInteraction)
        self.requestSystemEvent.connect(self.worker.systemEvent)
        self.requestCapabilities.connect(self.worker.probeCapabilities)
        self.requestKeys.connect(self.worker.listKeys)
        self.requestEnroll.connect(self.worker.enroll)
        self.requestAck.connect(self.worker.acknowledgeRecovery)
        self.requestShutdown.connect(self.worker.shutdown)
        self.requestWindowHandle.connect(self.worker.setWindowHandle)
        self.requestPrivilegedStatus.connect(self.worker.privilegedStatus)
        self.requestPrivilegedAction.connect(self.worker.privilegedAction)
        self.requestImportApp.connect(self.worker.importApp)
        self.requestRemoveKey.connect(self.worker.removeKey)
        self.requestMigrate.connect(self.worker.migrateVault)
        self.requestMigrateRecover.connect(self.worker.migrateRecover)

        self.filter = SessionEventFilter(self.requestSystemEvent.emit)
        QApplication.instance().installNativeEventFilter(self.filter)
        self.filter.register(int(self.winId()))
        self.requestWindowHandle.emit(int(self.winId()))

        self._shutdown_state = self.SHUTDOWN_IDLE
        self._shutdown_report = None
        self._shutdown_watchdog = QTimer(self)
        self._shutdown_watchdog.setSingleShot(True)
        self._shutdown_watchdog.timeout.connect(self._shutdown_timed_out)

        self.thread.start()
        self.requestBootstrap.emit()
        # Startup: ask the worker to MEASURE the privileged picture. Until the
        # report arrives the window shows EVALUATING and runs nothing heavy on
        # its own thread -- an ordinary status tick never measures anything.
        self.requestPrivilegedStatus.emit()

        self._tick_pending = False
        self._tick_coalesced = False
        self.timer = QTimer(self)
        self.timer.setInterval(2500)
        self.timer.timeout.connect(self._on_periodic_timer)
        self.timer.start()

    def _on_periodic_timer(self):
        if self._tick_pending:
            self._tick_coalesced = True
            return
        self._tick_pending = True
        self._tick_coalesced = False
        self.requestTick.emit()

    @pyqtSlot()
    def _tick_acknowledged(self):
        self._tick_pending = False
        if self._tick_coalesced:
            self._tick_coalesced = False
            self._tick_pending = True
            self.requestTick.emit()

    # ══════════════════════════════════════════════════════════ TAB 1: APPS
    def _build_apps_tab(self):
        layout = QVBoxLayout(self.tab_apps)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Profile selection table
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setFixedHeight(80)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table)

        # Status Grid Panel
        panel = QFrame()
        panel.setObjectName("panel")
        grid = QGridLayout(panel)
        grid.setVerticalSpacing(3)
        self.detail = {}
        labels = (
            "Status", "Vault", "Session", "Authentication", "Protected storage",
            "Session expiry", "Last activity", "Last lock reason",
            "Privileged helper", "Message"
        )
        for index, name in enumerate(labels):
            caption = QLabel(name + ":")
            caption.setObjectName("sub")
            value = QLabel("-")
            value.setWordWrap(True)
            value.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            grid.addWidget(caption, index, 0)
            grid.addWidget(value, index, 1)
            self.detail[name] = value
        layout.addWidget(panel)

        # Primary Controls Row
        ctrl_row = QHBoxLayout()
        self.btn_open = QPushButton("Open")
        self.btn_open.setFixedHeight(28)
        self.btn_goto_setup = QPushButton("Go to Setup & Security")
        self.btn_goto_setup.setFixedHeight(28)
        self.btn_goto_setup.setVisible(False)
        self.btn_lock = QPushButton("Lock now")
        self.btn_lock.setObjectName("danger")
        self.btn_lock.setFixedHeight(28)

        ctrl_row.addWidget(self.btn_open)
        ctrl_row.addWidget(self.btn_goto_setup)
        ctrl_row.addWidget(self.btn_lock)
        ctrl_row.addStretch()
        layout.addLayout(ctrl_row)

        self.btn_open.clicked.connect(self._open)
        self.btn_goto_setup.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        self.btn_lock.clicked.connect(self._lock)

        # Mode explanation box
        mode_box = QFrame()
        mode_box.setObjectName("panel")
        mode_layout = QVBoxLayout(mode_box)
        mode_layout.setSpacing(4)

        mode_header_row = QHBoxLayout()
        mode_header_row.addWidget(QLabel("Policy Mode:"))
        self.rb_default = QRadioButton("Default")
        self.rb_aggressive = QRadioButton("Aggressive")
        self.rb_default.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.rb_default)
        self.mode_group.addButton(self.rb_aggressive)
        mode_header_row.addWidget(self.rb_default)
        mode_header_row.addWidget(self.rb_aggressive)
        mode_header_row.addStretch()
        mode_layout.addLayout(mode_header_row)

        self.mode_desc = QLabel(DEFAULT_MODE_EXPLANATION)
        self.mode_desc.setObjectName("sub")
        self.mode_desc.setWordWrap(True)
        mode_layout.addWidget(self.mode_desc)
        layout.addWidget(mode_box)

        self.rb_default.toggled.connect(lambda c: self._on_mode_toggled("default") if c else None)
        self.rb_aggressive.toggled.connect(lambda c: self._on_mode_toggled("aggressive") if c else None)

    # ══════════════════════════════════════════════════════════ TAB 2: SETUP
    def _build_setup_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        main_layout = QVBoxLayout(self.tab_setup)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # Next Action Callout Card
        self.action_card = QFrame()
        self.action_card.setObjectName("action_card")
        ac_layout = QVBoxLayout(self.action_card)
        ac_layout.setSpacing(4)
        self.action_card_title = QLabel("NEXT ACTION: EVALUATING...")
        self.action_card_title.setObjectName("title")
        self.action_card_desc = QLabel("Reading system state...")
        self.action_card_desc.setWordWrap(True)
        self.action_card_btn = QPushButton("Execute Next Step")
        self.action_card_btn.setObjectName("primary_action")
        ac_layout.addWidget(self.action_card_title)
        ac_layout.addWidget(self.action_card_desc)
        ac_layout.addWidget(self.action_card_btn)
        layout.addWidget(self.action_card)
        self.action_card_btn.clicked.connect(self._run_current_next_action)

        # 8-step Ordered Checklist Table
        lbl_list = QLabel("SETUP PROGRESS CHECKLIST")
        lbl_list.setObjectName("title")
        layout.addWidget(lbl_list)

        self.checklist_table = QTableWidget(8, 3)
        self.checklist_table.setHorizontalHeaderLabels(["Step", "Status", "Summary"])
        self.checklist_table.verticalHeader().setVisible(False)
        self.checklist_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.checklist_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.checklist_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.checklist_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.checklist_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.checklist_table.setFixedHeight(190)
        layout.addWidget(self.checklist_table)

        # Step Actions Container
        lbl_ctrl = QLabel("STEP CONTROLS & DIAGNOSTICS")
        lbl_ctrl.setObjectName("title")
        layout.addWidget(lbl_ctrl)

        panel = QFrame()
        panel.setObjectName("panel")
        p_layout = QVBoxLayout(panel)
        p_layout.setSpacing(6)

        # Step 1 controls
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("1. Privilege separation:"))
        self.btn_prep_uac = QPushButton("Enable privilege separation")
        self.btn_restart_now = QPushButton("Restart now")
        self.btn_restart_later = QPushButton("Restart later")
        self.btn_restart_now.setVisible(False)
        self.btn_restart_later.setVisible(False)
        r1.addWidget(self.btn_prep_uac)
        r1.addWidget(self.btn_restart_now)
        r1.addWidget(self.btn_restart_later)
        r1.addStretch()
        p_layout.addLayout(r1)

        # Step 2 controls
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("2. Silent helper task:"))
        self.btn_install_task = QPushButton("Install")
        r2.addWidget(self.btn_install_task)
        self.lbl_uac_note = QLabel("Requires one-time Windows approval")
        self.lbl_uac_note.setObjectName("sub")
        r2.addWidget(self.lbl_uac_note)
        r2.addStretch()
        p_layout.addLayout(r2)

        # Step 3 controls
        r3 = QHBoxLayout()
        r3.addWidget(QLabel("3. Commissioning:"))
        self.btn_test_helper = QPushButton("Test silent helper")
        self.btn_helper_details = QPushButton("Details")
        self.btn_repair_task = QPushButton("Repair")
        self.btn_remove_task = QPushButton("Remove")
        self.btn_remove_task.setObjectName("danger")
        r3.addWidget(self.btn_test_helper)
        r3.addWidget(self.btn_helper_details)
        r3.addWidget(self.btn_repair_task)
        r3.addWidget(self.btn_remove_task)
        r3.addStretch()
        p_layout.addLayout(r3)

        self.lbl_helper_facts = QLabel("Helper: not checked | Broker: -")
        self.lbl_helper_facts.setObjectName("sub")
        p_layout.addWidget(self.lbl_helper_facts)

        # Step 4 controls
        r4 = QHBoxLayout()
        r4.addWidget(QLabel("4. YubiKey probe:"))
        self.btn_probe_key = QPushButton("Probe YubiKey")
        self.lbl_probe_facts = QLabel("Authenticator: not probed")
        self.lbl_probe_facts.setObjectName("sub")
        r4.addWidget(self.btn_probe_key)
        r4.addWidget(self.lbl_probe_facts)
        r4.addStretch()
        p_layout.addLayout(r4)

        # Step 5 controls
        r5 = QVBoxLayout()
        r5_header = QHBoxLayout()
        r5_header.addWidget(QLabel("5. Disposable acceptance:"))
        self.btn_run_acceptance = QPushButton("Run disposable acceptance")
        r5_header.addWidget(self.btn_run_acceptance)
        self.lbl_acc_disclaimer = QLabel("Uses temporary container. Real vault is untouched.")
        self.lbl_acc_disclaimer.setObjectName("sub")
        r5_header.addWidget(self.lbl_acc_disclaimer)
        r5_header.addStretch()
        r5.addLayout(r5_header)

        # One row per REAL acceptance check, straight from the runner's own
        # list. The state of a row is the state the runner reported for that
        # check, live, as the run reaches it.
        self.table_acceptance = QTableWidget(len(ACCEPTANCE_CHECKS), 2)
        self.table_acceptance.setHorizontalHeaderLabels(["Check", "Status"])
        self.table_acceptance.verticalHeader().setVisible(False)
        self.table_acceptance.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_acceptance.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_acceptance.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table_acceptance.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table_acceptance.setFixedHeight(220)
        for index, (check_id, label) in enumerate(ACCEPTANCE_CHECKS):
            name_item = QTableWidgetItem(label)
            name_item.setData(Qt.ItemDataRole.UserRole, check_id)
            self.table_acceptance.setItem(index, 0, name_item)
            self.table_acceptance.setItem(index, 1, QTableWidgetItem(ACCEPTANCE_IDLE))
        r5.addWidget(self.table_acceptance)
        self.lbl_acc_tokens = QLabel("")
        self.lbl_acc_tokens.setObjectName("sub")
        r5.addWidget(self.lbl_acc_tokens)
        p_layout.addLayout(r5)

        # Step 6 controls
        r6 = QHBoxLayout()
        r6.addWidget(QLabel("6. Obsidian application:"))
        self.btn_import_obsidian = QPushButton("Import portable Obsidian")
        r6.addWidget(self.btn_import_obsidian)
        self.lbl_import_dest = QLabel("Choose the folder containing Obsidian.exe")
        self.lbl_import_dest.setObjectName("sub")
        r6.addWidget(self.lbl_import_dest)
        r6.addStretch()
        p_layout.addLayout(r6)

        # Step 7 controls
        r7 = QHBoxLayout()
        r7.addWidget(QLabel("7. Encrypted vault:"))
        self.btn_enroll_vault = QPushButton("Create vault and enroll YubiKey")
        r7.addWidget(self.btn_enroll_vault)
        r7.addStretch()
        p_layout.addLayout(r7)

        # Step 8 controls
        r8 = QHBoxLayout()
        r8.addWidget(QLabel("8. Migration:"))
        self.btn_migrate_vault = QPushButton("Migrate vault")
        r8.addWidget(self.btn_migrate_vault)
        self.btn_open_backup = QPushButton("Open backup location")
        self.btn_open_backup.setVisible(False)
        r8.addWidget(self.btn_open_backup)
        r8.addStretch()
        p_layout.addLayout(r8)

        layout.addWidget(panel)

        # Button connects
        self.btn_prep_uac.clicked.connect(self._on_prep_uac)
        self.btn_restart_now.clicked.connect(self._restart_now)
        self.btn_restart_later.clicked.connect(self._restart_later)
        self.btn_install_task.clicked.connect(lambda: self._privileged_action("install"))
        self.btn_test_helper.clicked.connect(self._test_silent_helper)
        self.btn_helper_details.clicked.connect(self._show_helper_details)
        self.btn_repair_task.clicked.connect(lambda: self._privileged_action("repair"))
        self.btn_remove_task.clicked.connect(lambda: self._privileged_action("remove"))
        self.btn_probe_key.clicked.connect(lambda: self.requestCapabilities.emit(self._selected))
        self.btn_run_acceptance.clicked.connect(self._run_acceptance_dialog)
        self.btn_import_obsidian.clicked.connect(self._on_import_obsidian)
        self.btn_enroll_vault.clicked.connect(self._on_enroll_vault)
        self.btn_migrate_vault.clicked.connect(self._on_migrate_vault)
        self.btn_open_backup.clicked.connect(self._open_backup_location)

    # ══════════════════════════════════════════════════════════ TAB 3: RECOVERY
    def _build_recovery_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        main_layout = QVBoxLayout(self.tab_recovery)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # Section 1: Authentication Keys
        lbl_k = QLabel("AUTHENTICATION KEYS")
        lbl_k.setObjectName("title")
        layout.addWidget(lbl_k)

        panel_keys = QFrame()
        panel_keys.setObjectName("panel")
        pk_layout = QVBoxLayout(panel_keys)
        self.table_keys = QTableWidget(0, 4)
        self.table_keys.setHorizontalHeaderLabels(["Profile / Label", "Fingerprint", "Created", "Provider"])
        self.table_keys.verticalHeader().setVisible(False)
        self.table_keys.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_keys.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_keys.setFixedHeight(100)
        pk_layout.addWidget(self.table_keys)

        key_btn_row = QHBoxLayout()
        self.btn_add_key = QPushButton("Add backup key")
        self.btn_remove_key = QPushButton("Remove lost key")
        self.btn_remove_key.setObjectName("danger")
        key_btn_row.addWidget(self.btn_add_key)
        key_btn_row.addWidget(self.btn_remove_key)
        key_btn_row.addStretch()
        pk_layout.addLayout(key_btn_row)
        layout.addWidget(panel_keys)

        self.btn_add_key.clicked.connect(lambda: self.requestEnroll.emit(self._selected, True))
        self.btn_remove_key.clicked.connect(self._on_remove_lost_key)

        # Section 2: Storage Recovery
        lbl_sr = QLabel("STORAGE RECOVERY")
        lbl_sr.setObjectName("title")
        layout.addWidget(lbl_sr)

        panel_sr = QFrame()
        panel_sr.setObjectName("panel")
        psr_layout = QVBoxLayout(panel_sr)
        sr_desc = QLabel(
            "Reconcile a crashed or interrupted Secure Apps state and safely return\n"
            "protected storage to a known state. (This does not open the vault without YubiKey).")
        sr_desc.setObjectName("sub")
        psr_layout.addWidget(sr_desc)

        self.btn_recover_storage = QPushButton("Recover protected storage")
        psr_layout.addWidget(self.btn_recover_storage)
        layout.addWidget(panel_sr)
        self.btn_recover_storage.clicked.connect(lambda: self.requestRecover.emit(self._selected))

        # Section 3: Migration Recovery
        lbl_mr = QLabel("MIGRATION RECOVERY")
        lbl_mr.setObjectName("title")
        layout.addWidget(lbl_mr)

        panel_mr = QFrame()
        panel_mr.setObjectName("panel")
        pmr_layout = QVBoxLayout(panel_mr)
        mr_desc = QLabel("Inspect migration journal or safely recover an interrupted cutover.")
        mr_desc.setObjectName("sub")
        pmr_layout.addWidget(mr_desc)

        mr_btn_row = QHBoxLayout()
        self.btn_check_mig = QPushButton("Check migration state")
        self.btn_recover_mig = QPushButton("Recover interrupted migration")
        mr_btn_row.addWidget(self.btn_check_mig)
        mr_btn_row.addWidget(self.btn_recover_mig)
        mr_btn_row.addStretch()
        pmr_layout.addLayout(mr_btn_row)
        layout.addWidget(panel_mr)

        self.btn_check_mig.clicked.connect(self._check_migration_state)
        self.btn_recover_mig.clicked.connect(lambda: self.requestMigrateRecover.emit(self._selected))

        # Section 4: Emergency Instructions
        lbl_em = QLabel("EMERGENCY KEY LOSS INSTRUCTIONS")
        lbl_em.setObjectName("title")
        layout.addWidget(lbl_em)

        panel_em = QFrame()
        panel_em.setObjectName("panel")
        pem_layout = QVBoxLayout(panel_em)
        em_text = QLabel(
            "IF ONE KEY IS LOST:\n"
            "1. Use another enrolled YubiKey to open the vault normally.\n"
            "2. In this Recovery tab, select the lost key and click 'Remove lost key'.\n\n"
            "IF ALL YUBIKEYS ARE LOST:\n"
            "1. Locate the 48-digit BitLocker Recovery Password recorded during enrollment.\n"
            "2. Mount the container in Windows:\n"
            "   PowerShell (Admin): Mount-DiskImage -ImagePath \"%LOCALAPPDATA%\\SAITULS\\secure-apps\\vaults\\obsidian\\obsidian.vhdx\"\n"
            "3. Unlock with native BitLocker:\n"
            "   manage-bde -unlock X: -RecoveryPassword XXXXXX-XXXXXX-...\n"
            "4. Access files directly through Windows.\n\n"
            "IF NO YUBIKEY, NO RECOVERY PASSWORD AND NO BACKUP EXIST:\n"
            "Data cannot be recovered. There is no hidden backdoor or bypass."
        )
        em_text.setObjectName("sub")
        pem_layout.addWidget(em_text)
        layout.addWidget(panel_em)

    # ══════════════════════════════════════════════════════════ STATE REFRESH
    def _refresh_all(self):
        try:
            registry = sa_config.load_registry(self.registry_path, managed_root=self.managed_root)
            prof = registry.get(self._selected)
            self._last_acceptance = sa_acceptance.evaluate(registry.state_dir, prof)
        except Exception:
            pass

        self._refresh_setup()
        self.requestPrivilegedStatus.emit()
        self.requestCapabilities.emit(self._selected)
        self.requestKeys.emit(self._selected)
        self.requestTick.emit()

    def _load_registry(self):
        try:
            return sa_config.load_registry(self.registry_path, managed_root=self.managed_root)
        except Exception:
            return None

    def _resolve_reboot_pending(self, registry):
        """Derive the restart requirement from the durable marker.

        Cheap: a small file, the system uptime and the boot identity the
        marker recorded. The cached preflight report is passed along when one
        is available, for the legacy case of a marker written before boot
        identity existed -- a measured ``EnableLUA=1 + MEDIUM broker`` pair is
        NOT allowed to retire a marker written during the current boot, since
        that pair is exactly what a prepared-but-unrestarted host reports.
        """
        self._reboot_pending = bool(
            sa_setup.is_reboot_pending(registry.state_dir, self._last_preflight))
        return self._reboot_pending

    def _show_evaluating(self):
        """Honest state while the privileged picture has not been measured.

        The measurement is heavy (PowerShell, Task Scheduler, ACL probes). It
        runs on the broker worker and comes back as privilegedStatusReady; the
        window's own thread must never run it, not even once, so there is no
        code path from here to ``sa_privtask.preflight``.
        """
        self._current_next_action = None
        self.banner.setText("SECURE APPS: EVALUATING...")
        self.banner.setStyleSheet("color: %s;" % C_TEXT2)
        self.action_card_title.setText("NEXT ACTION: EVALUATING...")
        self.action_card_desc.setText(
            "Measuring privilege separation and the privileged helper. "
            "Setup steps activate when the measurement arrives.")
        self.action_card_btn.setText("Please wait")
        self.action_card_btn.setEnabled(False)
        self._gate_setup_buttons(None)

    def _gate_setup_buttons(self, action):
        """Bind every setup button to the one action that authorises it.

        Completed and future steps stay visible but disabled, so the checklist
        still reads as a whole while the only thing that can be clicked is the
        step the state machine is actually asking for.
        """
        for name, owner_action in SETUP_BUTTON_ACTIONS:
            button = getattr(self, name, None)
            if button is None:
                continue
            enabled = action == owner_action
            if name == "btn_run_acceptance" and self._acceptance_running:
                enabled = False
            button.setEnabled(enabled)

    def _require_setup_action(self, expected):
        """Fail closed when a handler is reached outside its setup state."""
        current = getattr(self, "_current_next_action", None)
        if current == expected:
            return True
        self._append_log(
            "refused: a step may only run when the next action is %s "
            "(current: %s)" % (expected, current or "none"))
        return False

    def _invalidate_setup(self, reason="", recompute=True):
        self._setup_snapshot_dirty = True
        self._last_acceptance = None
        if recompute:
            self._recompute_setup_state()

    def _refresh_setup(self):
        """Invalidate the cached setup snapshot and recompute it once.

        Every event that can change a setup-security fact routes here; the
        ordinary 2.5-second runtime tick never does (SRC-027 PERF-002).
        """
        self._invalidate_setup("refresh_setup", recompute=True)

    def _recompute_setup_state(self):
        if self._setup_snapshot is not None and not self._setup_snapshot_dirty:
            return
        registry = self._load_registry()
        if registry is None:
            return

        # The durable marker is authoritative and costs nothing to read, so it
        # is resolved before anything else is decided.
        self._resolve_reboot_pending(registry)

        if self._last_preflight is None and not self._reboot_pending:
            # No measurement yet: say so instead of guessing -- and never ask
            # sa_setup to measure it here, which is what put the preflight on
            # the GUI thread before.
            self._show_evaluating()
            self._setup_snapshot = {"state": "evaluating"}
            self._setup_snapshot_dirty = False
            return

        # An empty report is used only in the one case where nothing else can
        # be true: a durable restart requirement exists, and that alone decides
        # the next action no matter what the rest of the report would say.
        report = self._last_preflight or {}

        # PERF-002: the acceptance verdict is evaluated at most once per
        # recompute and shared by both setup functions; the enrollment and
        # migration-journal facts are likewise one CredentialStore construction
        # and one inspect per recompute, never rediscovered independently.
        profile = registry.get(self._selected)
        if self._last_acceptance is None and profile is not None:
            try:
                self._last_acceptance = sa_acceptance.evaluate(
                    registry.state_dir, profile)
            except Exception:
                self._last_acceptance = None
        try:
            store = sa_auth.CredentialStore(registry.credentials_path)
            enrolled_fact = bool(
                store.is_enrolled(self._selected)
                and profile is not None
                and os.path.isfile(profile.container))
        except Exception:
            enrolled_fact = False
        try:
            journal_path = ntpath.join(
                registry.state_dir, "migration-%s.json" % self._selected)
            migration_verdict = sa_migrate.inspect(journal_path).get("verdict")
        except Exception:
            migration_verdict = None

        action = sa_setup.get_next_setup_action(
            registry,
            profile_id=self._selected,
            preflight_report=report,
            commissioned=self._commissioned,
            probe_result=self._last_probe,
            acceptance_verdict=self._last_acceptance,
            reboot_pending=self._reboot_pending,
            enrolled_fact=enrolled_fact,
            migration_verdict=migration_verdict,
        )
        self._current_next_action = action
        self.action_card_btn.setEnabled(True)

        # Update Top Status Banner
        status = self._status_for(self._selected)
        if status and status.get("state") == sa_state.RECOVERY_REQUIRED:
            self.banner.setText("SECURE APPS: RECOVERY REQUIRED")
            self.banner.setStyleSheet("color: %s;" % C_DANGER_TEXT)
        elif action == sa_setup.ACTION_REBOOT_REQUIRED:
            self.banner.setText("SECURE APPS: REBOOT REQUIRED")
            self.banner.setStyleSheet("color: %s;" % C_BORDER_HL)
        elif action != sa_setup.ACTION_READY:
            self.banner.setText("SECURE APPS: SETUP REQUIRED")
            self.banner.setStyleSheet("color: %s;" % C_TEXT)
        elif status and status.get("state") == sa_state.RUNNING:
            self.banner.setText("SECURE APPS: RUNNING")
            self.banner.setStyleSheet("color: %s;" % C_BORDER_HL)
        elif status and status.get("state") == sa_state.LOCKED:
            self.banner.setText("SECURE APPS: LOCKED")
            self.banner.setStyleSheet("color: %s;" % C_TEXT2)
        else:
            self.banner.setText("SECURE APPS: READY")
            self.banner.setStyleSheet("color: %s;" % C_BORDER_HL)

        # Update Action Card
        action_titles = {
            sa_setup.ACTION_ENABLE_PRIVILEGE_SEPARATION: ("ENABLE PRIVILEGE SEPARATION", "Windows privilege separation is disabled. Click below to enable it.", "Enable privilege separation"),
            sa_setup.ACTION_REBOOT_REQUIRED: ("RESTART WINDOWS REQUIRED", "Windows must be restarted to apply privilege separation.", "Restart now"),
            sa_setup.ACTION_RUN_NON_ELEVATED: ("RUN AS STANDARD USER", "SAITULS is running elevated. Close it and launch normally.", "Close SAITULS"),
            sa_setup.ACTION_INSTALL_HELPER: ("INSTALL PRIVILEGED HELPER", "Install the one-time silent helper scheduled task.", "Install helper"),
            sa_setup.ACTION_COMMISSION_HELPER: ("VERIFY SILENT HELPER", "Test that Task Scheduler can start the helper without a UAC prompt.", "Test silent helper"),
            sa_setup.ACTION_PROBE_FIDO2: ("PROBE YUBIKEY", "Detect the connected FIDO2 security key and check hmac-secret support.", "Probe YubiKey"),
            sa_setup.ACTION_RUN_ACCEPTANCE: ("RUN DISPOSABLE ACCEPTANCE", "Run the temporary security acceptance test before protecting real data.", "Run disposable acceptance"),
            sa_setup.ACTION_IMPORT_APPLICATION: ("IMPORT PORTABLE OBSIDIAN", "Import Obsidian.exe into protected managed storage.", "Import Obsidian"),
            sa_setup.ACTION_ENROLL_PROFILE: ("CREATE ENCRYPTED VAULT", "Create the BitLocker VHDX and enroll your primary YubiKey.", "Create vault and enroll"),
            sa_setup.ACTION_MIGRATE_VAULT: ("MIGRATE OBSIDIAN NOTES", "Migrate plaintext notes into the verified encrypted vault.", "Migrate vault"),
            sa_setup.ACTION_READY: ("SETUP COMPLETE & READY", "All security gates passed. Obsidian is ready for secure use.", "Open Obsidian"),
        }
        t, d, b = action_titles.get(action, ("UNKNOWN", "State check", "Refresh"))
        self.action_card_title.setText("NEXT ACTION: %s" % t)
        self.action_card_desc.setText(d)
        self.action_card_btn.setText(b)

        # Update Checklist Table
        checklist = sa_setup.get_setup_checklist(
            registry,
            profile_id=self._selected,
            preflight_report=report,
            commissioned=self._commissioned,
            probe_result=self._last_probe,
            acceptance_verdict=self._last_acceptance,
            reboot_pending=self._reboot_pending,
            enrolled_fact=enrolled_fact,
            migration_verdict=migration_verdict,
        )
        for row, step in enumerate(checklist):
            self.checklist_table.setItem(row, 0, QTableWidgetItem(str(step["index"]) + ". " + step["title"]))
            status_item = QTableWidgetItem(step["status"])
            if step["status"] == sa_setup.STEP_COMPLETE:
                status_item.setForeground(Qt.GlobalColor.yellow)
            elif step["status"] in (sa_setup.STEP_ACTION_REQUIRED, sa_setup.STEP_FAILED):
                status_item.setForeground(Qt.GlobalColor.red)
            elif step["status"] == sa_setup.STEP_REBOOT_REQUIRED:
                status_item.setForeground(Qt.GlobalColor.yellow)
            self.checklist_table.setItem(row, 1, status_item)
            self.checklist_table.setItem(row, 2, QTableWidgetItem(step["summary"]))

        # Enable/Disable Open button in Apps tab
        if action != sa_setup.ACTION_READY:
            self.btn_open.setText("SETUP REQUIRED")
            self.btn_open.setEnabled(False)
            self.btn_goto_setup.setVisible(True)
        else:
            self.btn_open.setText("Open")
            self.btn_open.setEnabled(status and status.get("state") not in (
                sa_state.LOCKING, sa_state.AUTHENTICATING, sa_state.UNLOCKING, sa_state.RECOVERY_REQUIRED
            ))
            self.btn_goto_setup.setVisible(False)

        # Update Step 1 restart buttons
        if action == sa_setup.ACTION_REBOOT_REQUIRED or self._reboot_pending:
            self.btn_prep_uac.setVisible(False)
            self.btn_restart_now.setVisible(True)
            self.btn_restart_later.setVisible(True)
        else:
            self.btn_prep_uac.setVisible(True)
            self.btn_restart_now.setVisible(False)
            self.btn_restart_later.setVisible(False)

        # Every setup button, bound to the action that authorises it. The
        # action card is the state machine's voice; these are its hands.
        self._gate_setup_buttons(action)

        # PERF-002: the current setup-security facts are now rendered and
        # durable in the cache; a further tick sees the snapshot and skips the
        # whole registry/credential/journal re-read until the next invalidation.
        self._setup_snapshot = {
            "state": "ready",
            "action": action,
            "preflight": report,
            "commissioned": self._commissioned,
            "probe": self._last_probe,
            "reboot_pending": self._reboot_pending,
            "enrolled": enrolled_fact,
            "migration_verdict": migration_verdict,
        }
        self._setup_snapshot_dirty = False

    def _run_current_next_action(self):
        action = getattr(self, "_current_next_action", None)
        if action == sa_setup.ACTION_ENABLE_PRIVILEGE_SEPARATION:
            self._on_prep_uac()
        elif action == sa_setup.ACTION_REBOOT_REQUIRED:
            self._restart_now()
        elif action == sa_setup.ACTION_RUN_NON_ELEVATED:
            QMessageBox.information(
                self, "Non-Elevated Launch Required",
                "SAITULS is currently running with Administrator privileges.\n\n"
                "Please close this window and start SAITULS normally as a standard user.")
        elif action == sa_setup.ACTION_INSTALL_HELPER:
            self._privileged_action("install")
        elif action == sa_setup.ACTION_COMMISSION_HELPER:
            self._test_silent_helper()
        elif action == sa_setup.ACTION_PROBE_FIDO2:
            self.requestCapabilities.emit(self._selected)
        elif action == sa_setup.ACTION_RUN_ACCEPTANCE:
            self._run_acceptance_dialog()
        elif action == sa_setup.ACTION_IMPORT_APPLICATION:
            self._on_import_obsidian()
        elif action == sa_setup.ACTION_ENROLL_PROFILE:
            self.requestEnroll.emit(self._selected, False)
        elif action == sa_setup.ACTION_MIGRATE_VAULT:
            self._on_migrate_vault()
        elif action == sa_setup.ACTION_READY:
            self._open()

    # ══════════════════════════════════════════════════════════ ACTION HANDLERS
    def _on_prep_uac(self):
        if not self._require_setup_action(sa_setup.ACTION_ENABLE_PRIVILEGE_SEPARATION):
            return
        ans = QMessageBox.question(
            self, "Enable Privilege Separation",
            "Turn Windows privilege separation on (EnableLUA=1)?\n\n"
            "This will enable standard/administrator integrity separation.\n"
            "No other UAC setting is modified.\n"
            "Windows must be restarted afterward to apply.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.requestPrivilegedAction.emit("prepare-uac", 0)

    def _restart_now(self):
        ans = QMessageBox.question(
            self, "Restart Windows",
            "Restart Windows now to apply privilege separation?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            try:
                subprocess.run(["shutdown", "/r", "/t", "0"])
            except Exception as e:
                QMessageBox.warning(self, "Restart", "Could not trigger restart: %s" % e)

    def _restart_later(self):
        registry = self._load_registry()
        if registry is None:
            QMessageBox.warning(self, "Restart Later", "Could not read the Secure Apps registry.")
            return
        # Durable, and the durable marker is the ONLY thing the next launch
        # reads: this window keeps no separate completion truth of its own.
        try:
            sa_setup.mark_reboot_pending(registry.state_dir, True)
        except Exception as exc:
            self._append_log("restart-later marker could not be written: %s" % type(exc).__name__)
            QMessageBox.warning(
                self, "Restart Later",
                "The restart marker could not be written, so setup cannot \n"
                "remember this choice. Please restart Windows now.")
            self._resolve_reboot_pending(registry)
            self._invalidate_setup("restart_later")
            return
        self._resolve_reboot_pending(registry)
        QMessageBox.information(
            self, "Restart Later",
            "You chose to restart later. Setup is paused until Windows is restarted.\n"
            "When you restart and launch SAITULS normally, Setup will automatically resume.")
        self._invalidate_setup("restart_later")

    def _test_silent_helper(self):
        if not self._require_setup_action(sa_setup.ACTION_COMMISSION_HELPER):
            return
        self.requestPrivilegedAction.emit("check_start", 0)

    def _show_helper_details(self):
        if self._last_preflight is None:
            self._append_log("privileged status is still being measured; asking again")
            self.requestPrivilegedStatus.emit()
            QMessageBox.information(
                self, "Privileged Helper Details",
                "The privileged picture is still being measured. Try again in a moment.")
            return
        dialog = PrivilegedHelperDialog(self)
        dialog.apply_report(self._last_preflight)
        dialog.exec()

    def _on_enroll_vault(self):
        if not self._require_setup_action(sa_setup.ACTION_ENROLL_PROFILE):
            return
        self.requestEnroll.emit(self._selected, False)

    def _on_import_obsidian(self):
        if not self._require_setup_action(sa_setup.ACTION_IMPORT_APPLICATION):
            return
        source = QFileDialog.getExistingDirectory(
            self, "Choose portable Obsidian folder", os.path.expanduser("~"))
        if not source:
            return
        ans = QMessageBox.question(
            self, "Import Portable Obsidian",
            "Import portable Obsidian from:\n%s\n\ninto managed storage?" % source,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.lbl_import_dest.setText("Source: " + source)
            self.requestImportApp.emit(self._selected, source)

    # ── the visible acceptance table, driven by the runner's own checks ──
    def _reset_acceptance_table(self):
        self._acceptance_terminal = {}
        self._acceptance_cursor_id = None
        for row in range(self.table_acceptance.rowCount()):
            self.table_acceptance.setItem(row, 1, QTableWidgetItem(ACCEPTANCE_IDLE))

    def _set_acceptance_row(self, check_id, status):
        row = ACCEPTANCE_CHECK_ROW.get(check_id)
        if row is None:
            return
        item = QTableWidgetItem(status)
        if status == "PASS":
            item.setForeground(Qt.GlobalColor.yellow)
        elif status == "FAIL":
            item.setForeground(Qt.GlobalColor.red)
        elif status == ACCEPTANCE_RUNNING:
            item.setForeground(Qt.GlobalColor.white)
        else:
            item.setForeground(Qt.GlobalColor.gray)
        self.table_acceptance.setItem(row, 1, item)

    def _advance_acceptance_cursor(self, completed_id=None):
        """Show which check the run is on, so a silent wait is impossible."""
        if self._acceptance_cursor_id is not None and self._acceptance_cursor_id != completed_id:
            # Clear only a cursor that is still waiting; the check that just
            # reported now owns its own row and must not be reset to PENDING.
            self._set_acceptance_row(self._acceptance_cursor_id, ACCEPTANCE_IDLE)
        self._acceptance_cursor_id = None
        if completed_id is None:
            start = 0
        else:
            start = ACCEPTANCE_CHECK_ROW.get(completed_id, -1) + 1
        for check_id in ACCEPTANCE_CHECK_IDS[start:]:
            if check_id not in self._acceptance_terminal:
                self._set_acceptance_row(check_id, ACCEPTANCE_RUNNING)
                self._acceptance_cursor_id = check_id
                return

    @pyqtSlot(str, str, str)
    def _acceptance_check_outcome(self, check_id, outcome, detail):
        """One real check finished: paint the runner's verdict for it."""
        outcome = str(outcome or "").upper()
        if check_id not in ACCEPTANCE_CHECK_ROW or outcome not in ACCEPTANCE_OUTCOME_RANK:
            self._append_log("acceptance reported an unknown check: %r %r"
                             % (check_id, outcome))
            return
        previous = self._acceptance_terminal.get(check_id)
        if (previous is not None
                and ACCEPTANCE_OUTCOME_RANK[previous] > ACCEPTANCE_OUTCOME_RANK[outcome]):
            outcome = previous            # a FAIL is never repainted as a PASS
        self._acceptance_terminal[check_id] = outcome
        self._set_acceptance_row(check_id, outcome)
        self._advance_acceptance_cursor(check_id)
        self._append_log("  %-4s %s %s" % (outcome, check_id, detail))

    @pyqtSlot(object, str)
    def _confirm_request(self, response_queue, prompt_text):
        """Answer one security-relevant question, in the window's own thread.

        An unanswered question is refused: the dialog's default button is
        DO NOT APPROVE and a cancelled or failing dialog answers False, so no
        code path here can approve on the operator's behalf.
        """
        approved = False
        try:
            dialog = AcceptanceConfirmDialog(str(prompt_text), self)
            approved = dialog.exec() == QDialog.DialogCode.Accepted
        except Exception:
            approved = False
        finally:
            try:
                response_queue.put(bool(approved))
            except Exception:
                pass
        self._append_log("CONFIRM %s -> %s"
                         % (prompt_text, "APPROVED" if approved else "NOT APPROVED"))

    @pyqtSlot(object, str)
    def _pause_request(self, response_queue, prompt_text):
        """Hold the run at a physical step until the operator answers."""
        proceed = False
        try:
            dialog = PhysicalStepDialog(str(prompt_text), self)
            proceed = dialog.exec() == QDialog.DialogCode.Accepted
        except Exception:
            proceed = False
        finally:
            try:
                response_queue.put(bool(proceed))
            except Exception:
                pass
        self._append_log("PAUSE %s -> %s"
                         % (prompt_text, "CONTINUE" if proceed else "CANCELLED"))

    def _run_acceptance_dialog(self):
        if not self._require_setup_action(sa_setup.ACTION_RUN_ACCEPTANCE):
            return
        ans = QMessageBox.question(
            self, "Disposable Security Acceptance",
            "Run disposable security acceptance?\n\n"
            "This test uses a temporary 1 GiB container. Your real Obsidian vault is NOT touched.\n"
            "You will be prompted to enter your PIN, touch the key, and lock Windows (Win+L).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if ans != QMessageBox.StandardButton.Yes:
            return

        self._append_log("Starting disposable acceptance test...")
        self._acceptance_running = True
        self._reset_acceptance_table()
        self._advance_acceptance_cursor()
        self._gate_setup_buttons(self._current_next_action)
        self._acc_worker = AcceptanceWorker(self.registry_path, self.managed_root, self._selected, self)
        self._acc_worker.logMessage.connect(self._append_log)
        self._acc_worker.pinRequested.connect(self._prompt_pin)
        self._acc_worker.confirmRequested.connect(self._confirm_request)
        self._acc_worker.pauseRequested.connect(self._pause_request)
        self._acc_worker.checkOutcome.connect(self._acceptance_check_outcome)
        self._acc_worker.acceptanceFinished.connect(self._on_acceptance_worker_finished)
        self._acc_worker.start()

    def _on_acceptance_worker_finished(self, code):
        self._acceptance_running = False
        if getattr(self, "_close_after_acceptance", False):
            self._close_after_acceptance = False
            worker = self._acc_worker
            if worker is not None:
                worker.wait(5000)
            QTimer.singleShot(0, self.close)
        if self._acceptance_cursor_id is not None:
            self._set_acceptance_row(self._acceptance_cursor_id, ACCEPTANCE_IDLE)
            self._acceptance_cursor_id = None
        self._on_acceptance_finished()

    def _on_acceptance_finished(self):
        try:
            registry = sa_config.load_registry(self.registry_path, managed_root=self.managed_root)
            prof = registry.get(self._selected)
            self._last_acceptance = sa_acceptance.evaluate(registry.state_dir, prof)
        except Exception:
            pass

        passed = sorted(c for c, o in self._acceptance_terminal.items() if o == "PASS")
        failed = sorted(c for c, o in self._acceptance_terminal.items() if o == "FAIL")
        skipped = sorted(c for c, o in self._acceptance_terminal.items() if o == "SKIP")
        not_run = [c for c in ACCEPTANCE_CHECK_IDS if c not in self._acceptance_terminal]
        self._append_log(
            "acceptance run summary: %d passed, %d failed, %d skipped, %d not run"
            % (len(passed), len(failed), len(skipped), len(not_run)))
        for name, ids in (("failed", failed), ("skipped", skipped), ("not run", not_run)):
            if ids:
                self._append_log("  %s checks: %s" % (name, ", ".join(ids)))

        if self._last_acceptance and self._last_acceptance.ok:
            self.lbl_acc_tokens.setText("TOKENS: FIDO2_HARDWARE_ACCEPTED | STORAGE_ACCEPTED | MIGRATION_READY")
            self.lbl_acc_tokens.setStyleSheet("color: %s; font-weight: bold;" % C_BORDER_HL)
        else:
            self.lbl_acc_tokens.setText("Acceptance incomplete. Check reasons in log.")
            self.lbl_acc_tokens.setStyleSheet("color: %s;" % C_DANGER_TEXT)

        self._invalidate_setup("acceptance_finished")

    def _on_migrate_vault(self):
        if not self._require_setup_action(sa_setup.ACTION_MIGRATE_VAULT):
            return
        try:
            registry = sa_config.load_registry(self.registry_path, managed_root=self.managed_root)
            prof = registry.get(self._selected)
        except Exception as e:
            QMessageBox.warning(self, "Migration", str(e))
            return

        source = prof.mount_path
        ans = QMessageBox.question(
            self, "Confirm Vault Migration",
            "Migrate plaintext Obsidian vault?\n\n"
            "Source: %s\n"
            "Destination: Encrypted Obsidian VHDX\n\n"
            "NOTE: The original plaintext folder will NOT be deleted.\n"
            "It will be safely renamed aside and kept as a backup after verified cutover." % source,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.requestMigrate.emit(self._selected, source)

    def _open_backup_location(self):
        try:
            registry = sa_config.load_registry(self.registry_path, managed_root=self.managed_root)
            journal_path = ntpath.join(registry.state_dir, "migration-%s.json" % self._selected)
            data = sa_migrate.inspect(journal_path)
            archive = data.get("plaintext_archive") or data.get("source")
            if archive and os.path.isdir(archive):
                os.startfile(archive)
            else:
                QMessageBox.information(self, "Backup", "Backup path not found: %s" % archive)
        except Exception as e:
            QMessageBox.warning(self, "Backup", str(e))

    def _check_migration_state(self):
        try:
            registry = sa_config.load_registry(self.registry_path, managed_root=self.managed_root)
            journal_path = ntpath.join(registry.state_dir, "migration-%s.json" % self._selected)
            data = sa_migrate.inspect(journal_path)
            QMessageBox.information(
                self, "Migration State",
                "Phase verdict: %s\n"
                "Last step: %s\n"
                "Source: %s\n"
                "Plaintext present: %s\n"
                "Final mount present: %s" % (
                    data.get("verdict"), data.get("last_step"), data.get("source"),
                    data.get("plaintext_present"), data.get("final_mount_present")
                ))
        except Exception as e:
            QMessageBox.warning(self, "Migration State", str(e))

    def _on_remove_lost_key(self):
        if len(self._last_keys) <= 1:
            QMessageBox.warning(
                self, "Cannot Remove Key",
                "Refusing to remove the last enrolled credential.\n\n"
                "The vault would become unopenable except through its BitLocker recovery material.")
            return

        items = self.table_keys.selectedItems()
        if not items:
            QMessageBox.information(self, "Select Key", "Please select a key row to remove.")
            return
        row = items[0].row()
        label = self.table_keys.item(row, 0).text()
        item1 = self.table_keys.item(row, 1)
        fingerprint = (item1.data(Qt.ItemDataRole.UserRole) if item1 else None) or self.table_keys.item(row, 0).text()

        ans = QMessageBox.question(
            self, "Confirm Remove Key",
            "You are removing access for:\n%s (%s)\n\n"
            "This does not erase the physical YubiKey.\n"
            "It removes this credential from the Secure Apps profile.\n\n"
            "Removing requires authenticating with another still-valid key.\nProceed?" % (label, fingerprint),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.requestRemoveKey.emit(self._selected, fingerprint)

    def _copy_diagnostics(self):
        if self._last_preflight is None:
            self._append_log("diagnostics need the privileged measurement; asking for it")
            self.requestPrivilegedStatus.emit()
            QMessageBox.information(
                self, "Diagnostics",
                "The privileged picture is still being measured. Try again in a moment.")
            return
        try:
            registry = sa_config.load_registry(self.registry_path, managed_root=self.managed_root)
            status = self._status_for(self._selected)
            text = sa_setup.format_diagnostics(
                registry,
                profile_id=self._selected,
                preflight_report=self._last_preflight,
                acceptance_verdict=self._last_acceptance,
                probe_result=self._last_probe,
                status_payload=status
            )
            QApplication.clipboard().setText(text)
            self._append_log("Diagnostics copied to clipboard (secret-hygiene clean).")
            QMessageBox.information(self, "Diagnostics", "Diagnostics copied to clipboard.")
        except Exception as e:
            QMessageBox.warning(self, "Diagnostics", "Failed to build diagnostics: %s" % e)

    # ══════════════════════════════════════════════════════════ CORE SLOTS
    def _append_log(self, line):
        self.log.appendPlainText(line)

    def _fatal(self, message):
        self._append_log("FATAL " + message)
        QMessageBox.critical(self, "Secure Apps", "Secure Apps could not start:\n\n%s" % message)

    def _selection_changed(self):
        items = self.table.selectedItems()
        if items:
            self._selected = self.table.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole)
            self._refresh_detail()
            self._invalidate_setup("selection_changed")

    def _status_for(self, profile_id):
        for status in self._rows:
            if status["profile_id"] == profile_id:
                return status
        return None

    @pyqtSlot(list)
    def _apply_status(self, rows):
        if isinstance(rows, dict):
            rows = [rows]
        self._rows = rows or []
        keep = self._selected
        self.table.setRowCount(len(self._rows))
        for index, status in enumerate(self._rows):
            values = (
                status.get("label", status.get("profile_id", "App")),
                status.get("state", "UNKNOWN"),
                VAULT_TEXT.get(status.get("state", ""), "UNKNOWN"),
                ("ACTIVE, expires in %s" % human_seconds(status.get("session_expires_in_seconds", 0)))
                if status.get("session_active") else "NONE",
                human_seconds(status.get("session_idle_seconds", 0)) + " ago"
                if status.get("session_idle_seconds") is not None else "-",
                ("%d min" % status["idle_timeout_minutes"]) if "idle_timeout_minutes" in status else "-",
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, status.get("profile_id", ""))
                self.table.setItem(index, column, item)
            if keep is None:
                keep = status.get("profile_id")
        self._selected = keep
        for index, status in enumerate(self._rows):
            if status.get("profile_id") == keep:
                self.table.selectRow(index)
                break
        self._refresh_detail()
        self._recompute_setup_state()

    def _refresh_detail(self):
        status = self._status_for(self._selected)
        if status is None:
            return
        state = status.get("state", "UNKNOWN")
        self.detail["Status"].setText(STATE_TEXT.get(state, state))
        self.detail["Vault"].setText(VAULT_TEXT.get(state, "UNKNOWN"))
        if status.get("session_active"):
            sess = "ACTIVE (expires in %s)" % human_seconds(status.get("session_expires_in_seconds", 0))
        else:
            sess = "AUTH REQUIRED (no active session)"
        self.detail["Session"].setText(sess)
        self.detail["Authentication"].setText("YubiKey FIDO2 (hmac-secret + PIN)")
        self.detail["Protected storage"].setText(status.get("mount_path", "-"))
        self.detail["Session expiry"].setText(("%d minutes idle timeout" % status["idle_timeout_minutes"])
                                              if "idle_timeout_minutes" in status else "-")
        self.detail["Last activity"].setText(
            human_seconds(status.get("session_idle_seconds", 0)) + " ago"
            if status.get("session_idle_seconds") is not None else "-")
        self.detail["Last lock reason"].setText(status.get("last_lock_reason") or "-")
        self.detail["Privileged helper"].setText("HIGH integrity silent task" if self._commissioned else "Helper installed")

        msg = status.get("message") or status.get("last_error") or status.get("recovery_reason") or ""
        if not msg:
            msg = STATE_TEXT.get(state, state)
        self.detail["Message"].setText(msg or "-")
        self.btn_lock.setEnabled(True)

        mode = status.get("mode", "default")
        if mode == "aggressive":
            self.rb_aggressive.setChecked(True)
            self.mode_desc.setText(AGGRESSIVE_MODE_EXPLANATION)
        else:
            self.rb_default.setChecked(True)
            self.mode_desc.setText(DEFAULT_MODE_EXPLANATION)

    def _on_mode_toggled(self, mode):
        if mode == "aggressive":
            self.mode_desc.setText(AGGRESSIVE_MODE_EXPLANATION)
        else:
            self.mode_desc.setText(DEFAULT_MODE_EXPLANATION)
        if self._selected:
            self.requestMode.emit(self._selected, mode)

    def _open(self):
        if self._selected:
            self.requestInteraction.emit(self._selected)
            self.requestOpen.emit(self._selected)

    def _lock(self):
        if not self._selected:
            return
        ans = QMessageBox.question(
            self, "Lock Vault Now",
            "Lock '%s' now?\n\n"
            "This will close Obsidian, unmount the vault, destroy the cached session, "
            "and require YubiKey authentication for the next open." % self._selected,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if ans == QMessageBox.StandardButton.Yes:
            self.requestLock.emit(self._selected)
            self.btn_lock.setEnabled(False)
            self.detail["Message"].setText("LOCKING... closing application and securing storage")

    def _privileged_action(self, action):
        if action == "install" and not self._require_setup_action(sa_setup.ACTION_INSTALL_HELPER):
            return
        ans = QMessageBox.question(
            self, "Privileged Setup",
            "Execute privileged action '%s'?\n\nWindows may ask for elevation consent." % action,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.requestPrivilegedAction.emit(action, 0)

    @pyqtSlot(dict)
    def _privileged_status_ready(self, report):
        """A MEASURED preflight report. The only thing that fills the cache."""
        self._last_preflight = report if isinstance(report, dict) else {}
        registry = self._load_registry()
        if registry is not None:
            # A restart that has actually happened clears the durable marker
            # here, where the measurement that proves it is on hand.
            self._resolve_reboot_pending(registry)
        self._invalidate_setup("privileged_status_ready")

    @pyqtSlot(dict)
    def _tick_from_worker_direct(self):
        """Immediate fresh tick after long worker work — bypasses coalesce backlog."""
        if self._tick_pending:
            self._tick_coalesced = True
        else:
            self._tick_pending = True
            self._tick_coalesced = False
            self.requestTick.emit()

    def _privileged_action_result(self, payload):
        """The RESULT of one privileged mutation. Never a preflight report."""
        payload = payload if isinstance(payload, dict) else {}
        action = payload.get("action")
        ok = bool(payload.get("ok"))
        self._append_log("PRIVILEGED %s: %s" % (action, "OK" if ok else "FAIL"))

        if action == "prepare-uac" and ok:
            registry = self._load_registry()
            if registry is not None:
                try:
                    sa_setup.mark_reboot_pending(registry.state_dir, True)
                except Exception as exc:
                    self._append_log("restart marker could not be written: %s"
                                     % type(exc).__name__)
                self._resolve_reboot_pending(registry)
            QMessageBox.information(
                self, "Privilege Separation Prepared",
                "Windows privilege separation has been set (EnableLUA=1).\n\n"
                "Please RESTART Windows to apply changes.")
        elif action in ("check", "check_start"):
            commission = payload.get("commission") or {}
            if commission.get("ok"):
                self._commissioned = True
                self.lbl_helper_facts.setText("Broker: MEDIUM | Helper: HIGH | Silent launch: PASS | Task: VALID")
                self.lbl_helper_facts.setStyleSheet("color: %s; font-weight: bold;" % C_BORDER_HL)
                QMessageBox.information(
                    self, "Commissioning Passed",
                    "Silent privileged start verified!\n\n"
                    "Broker: MEDIUM integrity\n"
                    "Helper: HIGH integrity\n"
                    "Silent launch: PASS (No UAC prompt)\n"
                    "Task definition: VALID")
            else:
                self.lbl_helper_facts.setText("Commissioning failed: %s" % (commission.get("token") or "see log"))
                self.lbl_helper_facts.setStyleSheet("color: %s;" % C_DANGER_TEXT)
        elif action == "install":
            if ok:
                QMessageBox.information(
                    self, "Helper Installed",
                    "Silent privileged helper task installed successfully!\n"
                    "Now run 'Test silent helper' in Step 3.")
            else:
                QMessageBox.warning(self, "Install Failed", str(payload.get("message") or "Install failed"))
        elif action in ("repair", "remove") and not ok:
            QMessageBox.warning(
                self, "Privileged Helper",
                sa_setup.human_error_message(payload.get("error_category", ""),
                                             payload.get("message") or ""))

        # The worker requests a fresh measurement right after the mutation; the
        # next privilegedStatusReady carries it and recomputes the state. This
        # recompute only re-reads what is already known, so the window shows the
        # truthful "waiting for the fresh measurement" picture meanwhile.
        self._invalidate_setup("privileged_action_result")

    @pyqtSlot(dict)
    def _caps_ready(self, caps):
        self._last_probe = caps
        if caps.get("available") and caps.get("hmac_secret"):
            t = caps.get("transport", "ELEVATED_CTAP_HELPER")
            self.lbl_probe_facts.setText("YubiKey detected | FIDO2: YES | hmac-secret: YES | Transport: %s" % t)
            self.lbl_probe_facts.setStyleSheet("color: %s; font-weight: bold;" % C_BORDER_HL)
        else:
            self.lbl_probe_facts.setText("No compatible FIDO2 security key detected: %s" % (caps.get("detail") or "check connection"))
            self.lbl_probe_facts.setStyleSheet("color: %s;" % C_DANGER_TEXT)
        self._invalidate_setup("caps_ready")

    @pyqtSlot(str, list)
    def _keys_ready(self, profile_id, keys):
        self._last_keys = keys
        self.table_keys.setRowCount(len(keys))
        for row, k in enumerate(keys):
            cred_prof = str(k.get("credential_profile"))
            cred_hash = str(k.get("credential_id_hash"))
            self.table_keys.setItem(row, 0, QTableWidgetItem(cred_prof))
            item_hash = QTableWidgetItem(cred_hash[:16] + "...")
            item_hash.setData(Qt.ItemDataRole.UserRole, cred_hash or cred_prof)
            self.table_keys.setItem(row, 1, item_hash)
            self.table_keys.setItem(row, 2, QTableWidgetItem(str(k.get("created"))))
            self.table_keys.setItem(row, 3, QTableWidgetItem(str(k.get("provider"))))
        self._invalidate_setup("keys_ready")

    @pyqtSlot(str, str)
    def _show_recovery(self, profile_id, recovery):
        dialog = RecoveryDialog(profile_id, recovery, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self.requestAck.emit(accepted)

    @pyqtSlot(object)
    def _prompt_pin(self, response_queue):
        dialog = PinDialog(self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                response_queue.put(dialog.get_pin())
            else:
                dialog.entry.clear()
                response_queue.put(None)
        except Exception:
            response_queue.put(None)

    @pyqtSlot(dict)
    def _enroll_done(self, payload):
        self._append_log("ENROLL %s" % payload)
        if payload.get("ok"):
            QMessageBox.information(self, "Vault Enrolled", payload.get("reason") or "Vault created and key enrolled.")
        else:
            QMessageBox.warning(self, "Enrollment Failed", payload.get("reason") or "Enrollment refused.")
        if self._selected:
            self.requestKeys.emit(self._selected)
        self._invalidate_setup("enroll_done")

    @pyqtSlot(dict)
    def _apply_operation(self, payload):
        was_long = payload.get("action") in (
            "migrate", "import_app", "enroll", "remove_key") or payload.get("ok") is not None
        ok = payload.get("ok")
        action = payload.get("action", "")
        reason = payload.get("reason", "")
        self._append_log("%s %s: %s" % ("OK  " if ok else "FAIL", action or payload.get("profile_id"), reason))
        if action == "migrate" and ok:
            self.btn_open_backup.setVisible(True)
            QMessageBox.information(
                self, "Migration Complete",
                "Obsidian protected: YES\nPlaintext backup still exists: YES\n\n"
                "Your encrypted vault is active, but the old plaintext backup can still be read "
                "without YubiKey authentication.\n\n"
                "Use 'Open backup location' to review or archive it.")
        elif not ok and payload.get("error_category") not in ("none", "already_running", "concurrent_request"):
            cat = payload.get("error_category", "")
            human_msg = sa_setup.human_error_message(cat, reason)
            QMessageBox.warning(self, "Secure Apps", human_msg)
        if not ok and self._privileged_relevant(payload, reason):
            self._append_log("privileged picture may have changed; re-measuring")
            self.requestPrivilegedStatus.emit()
        self._invalidate_setup("operation_applied")
        # Trigger one fresh status tick after long operations so expiry/exit
        # policy resumes immediately instead of draining stale queued ticks.
        if was_long:
            self._tick_from_worker_direct()

    def _privileged_relevant(self, payload, reason):
        blob = ("%s %s" % (payload.get("error_category") or "", reason or "")).lower()
        return any(token in blob for token in self.PRIVILEGED_REFRESH_TOKENS)

    # ══════════════════════════════════════════════════════════ SHUTDOWN BARRIER
    def closeEvent(self, event):
        if self._shutdown_state == self.SHUTDOWN_DONE:
            event.accept()
            return
        worker = self._acc_worker
        if worker is not None and worker.isRunning():
            # A running acceptance owns a disposable container: cancel it and
            # close only once its thread has actually finished (CORE-001).
            worker.cancel()
            self._close_after_acceptance = True
            self._append_log("shutdown: cancelling the running acceptance first...")
            event.ignore()
            return
        if self._shutdown_state == self.SHUTDOWN_RUNNING:
            event.ignore()
            return
        if self._shutdown_state == self.SHUTDOWN_FAILED:
            event.ignore()
            QMessageBox.critical(
                self, "Secure Apps - RECOVERY REQUIRED",
                "One or more protected volumes did not detach cleanly.\n"
                "Recovery is required before exiting.")
            return

        self._shutdown_state = self.SHUTDOWN_RUNNING
        self.setEnabled(False)
        self.setWindowTitle("Secure Apps - closing safely...")
        self._append_log("shutdown: securing vaults before exit...")

        self._shutdown_watchdog.start(self.SHUTDOWN_TIMEOUT_MS)
        self.requestShutdown.emit()
        event.ignore()

    @pyqtSlot(dict)
    def _shutdown_done(self, report):
        self._shutdown_watchdog.stop()
        self._shutdown_report = report

        if not report.get("ok"):
            self._shutdown_state = self.SHUTDOWN_FAILED
            self.setEnabled(True)
            self.setWindowTitle("SAITULS - Secure Apps [RECOVERY REQUIRED]")
            self._append_log("FATAL: shutdown failed: %s" % report.get("reason"))
            QMessageBox.critical(
                self, "Secure Apps - RECOVERY REQUIRED",
                "Secure shutdown failed. One or more vaults did not detach cleanly:\n\n%s"
                % (report.get("reason") or "recovery required"))
            return

        self._shutdown_state = self.SHUTDOWN_DONE

        if self.filter is not None:
            try:
                self.filter.unregister()
                QApplication.instance().removeNativeEventFilter(self.filter)
            except Exception:
                pass
            self.filter = None

        self.thread.quit()
        self.thread.wait(self.THREAD_JOIN_MS)
        self.close()

    def _shutdown_timed_out(self):
        self._shutdown_done({
            "ok": False,
            "reason": "watchdog timeout: the broker did not finish locking within %d seconds"
                      % (self.SHUTDOWN_TIMEOUT_MS // 1000),
            "recovery_required": [self._selected] if self._selected else [],
        })


def parse_gui_args(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="SAITULS Secure Apps GUI")
    parser.add_argument("--registry", default=None, help="Path to registry.json")
    parser.add_argument("--managed-root", default=None, help="Path to managed root directory")
    parser.add_argument("--profile", default=None, help="Initial profile to select")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_gui_args(argv)
    guard = sa_broker.SingleInstanceGuard()
    if not guard.acquire():
        app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
        app.setStyleSheet(QSS)
        QMessageBox.information(
            None, "Secure Apps Already Running",
            "Another instance of Secure Apps or its broker is already active.\n"
            "Please switch to the existing window.")
        return 0

    app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    app.setStyleSheet(QSS)
    reg_path = args.registry or sa_config.default_registry_path()
    window = SecureAppsWindow(reg_path, managed_root=args.managed_root, initial_profile=args.profile)
    window.show()
    try:
        return app.exec()
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
