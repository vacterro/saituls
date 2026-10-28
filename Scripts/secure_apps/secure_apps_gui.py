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
import ctypes
import ntpath
import os
import queue
import secrets
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
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QAbstractItemView, QDialog, QDialogButtonBox, QPlainTextEdit, QLineEdit,
    QMessageBox, QListWidget, QSizePolicy, QTabWidget, QRadioButton, QButtonGroup,
    QScrollArea, QFileDialog, QProgressBar, QCheckBox
)

import sa_acceptance
import sa_acceptance_runner
import sa_audit
import sa_auth
import sa_broker
import sa_capacity
import sa_config
import sa_crypto
import sa_enroll
import sa_migrate
import sa_mnemonic
import sa_privtask
import sa_recovery
import sa_setup
import sa_state
import sa_win

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
QFrame#panel QLabel, QFrame#action_card QLabel { background-color: transparent; }
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
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; width: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: %(surface)s; }
QScrollBar:horizontal { background: %(surface)s; height: 14px; }
QScrollBar::handle:horizontal { background: %(raised)s; border: 1px solid %(dark)s; min-width: 20px; }
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
QProgressBar { background-color: %(compare)s; color: %(hl)s; border: 2px solid %(dark)s;
               border-bottom-color: %(bevel)s; border-right-color: %(bevel)s;
               min-height: 14px; max-height: 14px; text-align: center; font-weight: bold; }
QProgressBar::chunk { background-color: %(bevel)s; }
QPushButton#advanced_toggle { background-color: transparent; color: %(text2)s; border: none;
                              padding: 2px 0px; text-align: left; }
QPushButton#advanced_toggle:hover { color: %(hl)s; }
""" % {"bg": C_BG, "bgsoft": C_BG_SOFT, "surface": C_SURFACE, "raised": C_RAISED,
       "alt": C_ALT, "dark": C_BORDER_DARK, "hl": C_BORDER_HL, "bevel": C_BEVEL_LIGHT,
       "bordermuted": C_BORDER_MUTED, "text": C_TEXT, "text2": C_TEXT2,
       "muted": C_MUTED, "dangertext": C_DANGER_TEXT, "sel": C_SELECTION,
       "compare": C_COMPARE}

def apply_golden_font(app):
    """UI.md iron law 1: Verdana, NON-antialiased, everywhere -- no blur.

    The QSS sets family and size only; Qt has no stylesheet property for
    antialiasing, so without this the text is smoothed (grey fringe pixels).
    The strategy lives on the application font, which every widget and every
    QMessageBox/QDialog resolves its stylesheet font against.
    """
    if app is None:
        return
    font = QFont("Verdana")
    font.setPixelSize(11)
    font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app.setFont(font)


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
    ("btn_open_backup", sa_setup.ACTION_SECURE_PLAINTEXT_BACKUP),
)


# What the person will actually see when they press Continue on the action
# card -- which prompts appear and what is (not) touched -- so the one button
# can be pressed without first reading the eight-step checklist.
SETUP_ACTION_HINTS = {
    sa_setup.ACTION_ENABLE_PRIVILEGE_SEPARATION:
        "Windows asks for administrator approval once; a restart follows.",
    sa_setup.ACTION_REBOOT_REQUIRED:
        "Save your work first. Setup continues here after Windows starts again.",
    sa_setup.ACTION_RUN_NON_ELEVATED:
        "Start SAITULS from its normal shortcut, not 'Run as administrator'.",
    sa_setup.ACTION_INSTALL_HELPER:
        "Windows asks for administrator approval once. After this, opening and "
        "locking the vault never shows a UAC prompt again.",
    sa_setup.ACTION_COMMISSION_HELPER:
        "No prompts: checks that the helper starts silently.",
    sa_setup.ACTION_PROBE_FIDO2:
        "Plug in your YubiKey first. No PIN is asked at this step.",
    sa_setup.ACTION_RUN_ACCEPTANCE:
        "Uses a throw-away test container; your notes are not touched. You will "
        "enter the YubiKey PIN, touch the key and lock Windows once (Win+L).",
    sa_setup.ACTION_IMPORT_APPLICATION:
        "Pick the folder that contains your portable Obsidian.exe. It is copied, "
        "not moved.",
    sa_setup.ACTION_ENROLL_PROFILE:
        "Enter the YubiKey PIN and touch the key. A BitLocker recovery password "
        "is then shown once -- write it down: it is the only way in if every "
        "key is lost.",
    sa_setup.ACTION_MIGRATE_VAULT:
        "Copies your notes into the encrypted vault and verifies them. The "
        "plaintext folder is renamed aside as a backup, never deleted.",
    sa_setup.ACTION_SECURE_PLAINTEXT_BACKUP:
        "Opens the backup folder so you can look at it before you remove it. "
        "SAITULS never deletes your notes for you -- the copy stays readable "
        "to anyone signed in to Windows until you do.",
    sa_setup.ACTION_READY: "",
}


# RUN_NON_ELEVATED when the cause is Explorer, not the launch: (title,
# description, button, hint). Signing out is the user's call -- the button
# explains, it never logs anyone off.
SHELL_ELEVATED_CARD = (
    "SIGN OUT AND BACK IN",
    "Windows Explorer itself is running as administrator, so every shortcut "
    "starts SAITULS elevated -- launching it 'normally' cannot fix this.",
    "How to fix",
    "Sign out of Windows and sign back in (or restart). Explorer then starts "
    "normally; open SAITULS from its shortcut and continue here.",
)
# Explorer elevated with NO split token while EnableLUA=1: UAC was switched
# on after the last boot and only a Windows restart activates it -- signing
# out or restarting Explorer keeps the same full token (measured live).
UAC_INACTIVE_CARD = (
    "RESTART WINDOWS",
    "User Account Control is switched on but not active yet: it was enabled "
    "after Windows last started, so everything -- Explorer and every shortcut "
    "-- still runs as administrator.",
    "How to fix",
    "Restart Windows when your work allows. Signing out is not enough. "
    "Then open SAITULS from its shortcut and continue here.",
)
UAC_INACTIVE_EXPLANATION = (
    "User Account Control (EnableLUA) is on in the registry, but Windows only "
    "applies it at startup, and it was turned on after the last restart.\n\n"
    "Until Windows restarts, every program -- Explorer, the taskbar and "
    "anything started from a shortcut -- runs with full administrator "
    "rights, and Secure Apps refuses to run that way.\n\n"
    "Fix: restart Windows when your work allows (signing out or restarting "
    "Explorer is not enough). Then open SAITULS from its usual shortcut.")
SHELL_ELEVATED_EXPLANATION = (
    "Windows Explorer (the taskbar, desktop and Start menu) is running with "
    "administrator rights. That usually happens when Explorer is restarted "
    "from a program that was itself running as administrator.\n\n"
    "While it stays that way, everything you start from a shortcut is "
    "elevated too, and Secure Apps refuses to run elevated.\n\n"
    "Fix: save your work, sign out of Windows and sign back in (or restart). "
    "Then open SAITULS from its usual shortcut.")


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
    # T-256 capacity truth: MEASURED container facts (never a guess) and the
    # result of one plan evaluation. Both are emitted from the worker thread
    # because virtdisk, disk_usage and the Hyper-V toolchain probe are I/O.
    capacityReady = pyqtSignal(dict)
    growPlanReady = pyqtSignal(dict)

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

    # ── T-256 capacity: measured on the worker, never on the UI thread ──
    def _profile_capacity_facts(self, profile_id):
        registry = sa_config.load_registry(self.registry_path,
                                           managed_root=self.managed_root)
        profile = registry.get(profile_id)
        container = getattr(profile, "container", None) or ""
        provisioned = getattr(profile, "size_gb", None)
        facts = sa_capacity.container_facts(container, provisioned)
        facts["verdicts"] = sa_capacity.classify(facts)
        return facts

    @pyqtSlot(str)
    def capacityFacts(self, profile_id):
        """Report what the container really is. Mounts nothing, changes nothing."""
        payload = {"profile_id": profile_id, "ok": False, "facts": None,
                   "verdicts": [], "toolchain": None, "detail": None}
        try:
            facts = self._profile_capacity_facts(profile_id)
            # The toolchain probe spawns PowerShell once per process and is
            # cached; a growth that cannot run must say so in the panel rather
            # than at the end of a plan request.
            toolchain = sa_capacity.hyperv_toolchain()
            payload["facts"] = facts
            payload["verdicts"] = facts["verdicts"]
            payload["toolchain"] = toolchain
            payload["ok"] = True
        except Exception as exc:
            payload["detail"] = "%s: %s" % (type(exc).__name__, exc)
        self.capacityReady.emit(payload)

    @pyqtSlot(str, int)
    def planGrow(self, profile_id, new_gb):
        """Evaluate a grow plan. Read-only: this never resizes anything."""
        payload = {"profile_id": profile_id, "new_gb": new_gb, "ok": False,
                   "plan": None, "refusal": None, "checks": [],
                   "procedure": sa_capacity.grow_procedure(), "facts": None,
                   "toolchain": None, "detail": None}
        try:
            facts = self._profile_capacity_facts(profile_id)
            toolchain = sa_capacity.hyperv_toolchain()
            payload["facts"] = facts
            payload["toolchain"] = toolchain
            payload["checks"] = sa_capacity.grow_checks(
                facts["container"], facts["provisioned_gb"], new_gb,
                facts=facts, toolchain=toolchain)
            plan, refusal = sa_capacity.plan_grow(
                facts["container"], facts["provisioned_gb"], new_gb,
                facts=facts, toolchain=toolchain)
            payload["plan"] = plan
            payload["refusal"] = refusal
            payload["ok"] = refusal is None
        except Exception as exc:
            payload["detail"] = "%s: %s" % (type(exc).__name__, exc)
        self.growPlanReady.emit(payload)

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
    @pyqtSlot(str, bool, object)
    def enroll(self, profile_id, additional, portable_password=None):
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
                    timeout_seconds=self._recovery_ack_timeout,
                    portable_password=portable_password)
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
    @pyqtSlot(str, str, str)
    def changePortablePassword(self, profile_id, new_password, old_password=None):
        if self.broker is None:
            return
        try:
            res = self.broker.change_portable_password(profile_id, new_password, old_password=old_password)
            self.operationDone.emit({
                "ok": res.get("ok", False),
                "profile_id": profile_id,
                "action": "change_portable_password",
                "reason": res.get("reason", "Portable password updated" if res.get("ok") else "Failed to update portable password")
            })
        except Exception as exc:
            self.operationDone.emit({
                "ok": False, "profile_id": profile_id, "action": "change_portable_password",
                "reason": str(exc), "error_category": getattr(exc, "category", "internal")
            })
        self.push_status()

    @pyqtSlot(str)
    def removePortablePassword(self, profile_id):
        if self.broker is None:
            return
        try:
            res = self.broker.remove_portable_password(profile_id)
            self.operationDone.emit({
                "ok": res.get("ok", False),
                "profile_id": profile_id,
                "action": "remove_portable_password",
                "reason": res.get("reason", "Portable password removed" if res.get("ok") else "Failed to remove portable password")
            })
        except Exception as exc:
            self.operationDone.emit({
                "ok": False, "profile_id": profile_id, "action": "remove_portable_password",
                "reason": str(exc), "error_category": getattr(exc, "category", "internal")
            })
        self.push_status()

    @pyqtSlot(str)
    def listProtectors(self, profile_id):
        if self.broker is None:
            return
        try:
            res = self.broker.list_protectors(profile_id)
            self.operationDone.emit({
                "ok": res.get("ok", False),
                "profile_id": profile_id,
                "action": "list_protectors",
                "protectors": res.get("protectors", []),
                "reason": res.get("reason", "BitLocker protectors listed")
            })
        except Exception as exc:
            self.operationDone.emit({
                "ok": False, "profile_id": profile_id, "action": "list_protectors",
                "reason": str(exc), "error_category": getattr(exc, "category", "internal")
            })
        self.push_status()

    @pyqtSlot(str)
    def publishBackup(self, profile_id):
        if self.broker is None:
            return
        try:
            res = self.broker.publish_backup(profile_id)
            self.operationDone.emit({
                "ok": res.get("ok", False),
                "profile_id": profile_id,
                "action": "publish_backup",
                "reason": res.get("reason", "Encrypted backup published successfully" if res.get("ok") else "Backup failed")
            })
        except Exception as exc:
            self.operationDone.emit({
                "ok": False, "profile_id": profile_id, "action": "publish_backup",
                "reason": str(exc), "error_category": getattr(exc, "category", "internal")
            })
        self.push_status()

    @pyqtSlot()
    def verifyBackup(self):
        try:
            hmac_key = None
            if self.broker is not None:
                for runtime in self.broker._runtimes.values():
                    if runtime.session and runtime.session.secret:
                        try:
                            hmac_key = sa_crypto.derive_manifest_hmac_key(
                                runtime.session.secret, runtime.profile.container_id
                            )
                            break
                        except Exception:
                            pass
            res = sa_backup.verify_backup(hmac_key=hmac_key)
            if res.get("ok"):
                sha = (res.get("actual_sha256") or res.get("sha256") or "")[:16] + "..."
                payload_ok = res.get("payload_sha256_verified", False)
                sidecar_status = res.get("sidecar_authenticated", "UNVERIFIED")

                if sidecar_status == "VERIFIED":
                    sidecar_desc = "VERIFIED"
                elif sidecar_status == "UNVERIFIED":
                    sidecar_desc = "UNVERIFIED — YubiKey verification required"
                elif sidecar_status == "NO_HMAC":
                    sidecar_desc = "UNVERIFIED (No HMAC in manifest)"
                elif sidecar_status == "NO_METADATA":
                    sidecar_desc = "NO METADATA"
                else:
                    sidecar_desc = sidecar_status

                msg = (
                    "Backup verification result:\n\n"
                    "Payload bytes:\n%s\n\n"
                    "Sidecar authenticity:\n%s\n\n"
                    "Vault: %s\n"
                    "SHA-256: %s"
                ) % (
                    "VERIFIED" if payload_ok else "FAILED",
                    sidecar_desc,
                    res.get("vault_path", ""),
                    sha
                )
                self.operationDone.emit({
                    "ok": True,
                    "action": "verify_backup",
                    "reason": msg,
                    "payload_verified": payload_ok,
                    "sidecar_authenticated": sidecar_status,
                })
            else:
                self.operationDone.emit({
                    "ok": False,
                    "action": "verify_backup",
                    "reason": res.get("error") or res.get("reason", "Backup verification failed"),
                    "error_category": "internal"
                })
        except Exception as exc:
            self.operationDone.emit({
                "ok": False,
                "action": "verify_backup",
                "reason": str(exc),
                "error_category": "internal"
            })
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


# ══════════════════════════════════════════════════════════ layout & localization helpers
def get_current_keyboard_layout():
    """Detect current Windows keyboard layout and return a friendly string."""
    try:
        user32 = ctypes.windll.user32
        hkl = user32.GetKeyboardLayout(0)
        lid = hkl & 0xFFFF
        names = {
            0x0409: "EN (US English)",
            0x0809: "EN (UK English)",
            0x0419: "RU (Русский)",
            0x0425: "ET (Eesti)",
            0x0407: "DE (Deutsch)",
            0x040C: "FR (Français)",
            0x0410: "IT (Italiano)",
            0x040A: "ES (Español)",
            0x0422: "UK (Українська)",
        }
        return names.get(lid, "0x%04X" % lid)
    except Exception:
        return "Unknown"


def is_caps_lock_on():
    """Detect whether Caps Lock is currently toggled on."""
    try:
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)
    except Exception:
        return False


def get_system_ui_language():
    """Detect Windows OS UI display language: 'ru', 'et', or 'en'."""
    try:
        if os.name == "nt":
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            if lang_id == 0x19:
                return "ru"
            if lang_id == 0x25:
                return "et"
    except Exception:
        pass
    try:
        from PyQt6.QtCore import QLocale
        lang = QLocale.system().language()
        if lang == QLocale.Language.Russian:
            return "ru"
        if lang == QLocale.Language.Estonian:
            return "et"
    except Exception:
        pass
    return "en"


STRINGS = {
    "en": {
        "pin_title": "Secure Apps - Security Key PIN",
        "pin_prompt": "Enter FIDO2 Security Key PIN:",
        "pin_guide": "👉 After entering PIN and clicking OK: TOUCH YOUR YUBIKEY when it flashes!\n⚠️ During key registration, touch TWICE (2x in a row).",
        "pin_layout": "🌐 Layout: %s",
        "pin_caps": "⚠️ CAPS LOCK IS ON!",
        "pin_show": "Show PIN",
        "pin_ok": "OK (Confirm PIN)",
        "banner_enter_pin_title": "🔑 ENTER PIN IN DIALOG (then touch key)",
        "banner_enter_pin_detail": "Enter your FIDO2 PIN in the dialog and click OK. The YubiKey will then flash for touch confirmation.",
        "banner_touch_now_title": "👉 TOUCH YUBIKEY NOW! (Device is blinking)",
        "banner_touch_now_detail": "Touch the gold contact on the key to authorize the operation.",
        "banner_touch_twice_title": "👉 TOUCH YUBIKEY TWICE (2x)!",
        "banner_touch_twice_detail": "1. First touch: create credential (FIDO2)\n2. Second touch: bind BitLocker key\nTouch the key both times it flashes!",
        "banner_lock_title": "🔒 LOCK WINDOWS (Press Win + L)!",
        "banner_lock_detail": "Lock Windows (Win+L), wait 3-5 seconds, then log back in.",
        "banner_container_title": "📦 Preparing BitLocker VHDX container...",
        "banner_container_detail": "This usually takes 10-20 seconds. Please wait...",
        "banner_waiting": "⏳ Please wait...",
        "banner_operation": "⏳ Operation in progress... Please wait",
        "step_dialog_title": "Secure Apps - Physical Step Required",
        "step_dialog_head": "PHYSICAL ACTION REQUIRED",
        "step_dialog_note": "The test paused for a physical security key action (unplug/plug/lock).\nYour real Obsidian vault is safe and untouched.",
        "rec_title": "Secure Apps - BitLocker Recovery Material",
        "rec_head": "BitLocker recovery material for '%s'",
        "rec_warn": "Save this BitLocker recovery password offline.\n\nIt can recover your data if every enrolled YubiKey is lost.\n\nSAITULS does not store this password.",
        "rec_tab_num": "Numeric Key (48 digits)",
        "rec_tab_mnemonic": "12-Word Mnemonic Phrase",
        "rec_tab_custom": "Custom Passphrase (6-12 Words)",
        "rec_copy_words_30s": "Copy 12 Words for 30 seconds",
        "rec_copied_title": "Copied for 30 seconds",
        "rec_copied_desc": "The recovery material is on the clipboard and will be cleared automatically after 30 seconds.\n\nTwo things a clipboard does that paper does not:\n  - Windows clipboard history (Win+V) keeps what you copy, and can sync it to other signed-in devices.\n  - Cloud clipboard features may upload it off this machine.\n\nIf either is on, prefer typing the key by hand, or write it down. Leaving it on the clipboard indefinitely is not safe.",
        "rec_mnemonic_desc": "This 12-word phrase is mathematically equivalent to the 48-digit BitLocker key.\nYou can write down these 12 words instead of the 48 digits.",
        "rec_mnemonic_na": "12-word mnemonic is available for standard BitLocker keys.",
        "rec_custom_prompt": "Optional: Enter your own 6 to 12 words master recovery passphrase:",
        "rec_custom_hint": "6-12 words provide an extra human-readable offline recovery option.",
    },
    "ru": {
        "pin_title": "Secure Apps - PIN-код ключа FIDO2",
        "pin_prompt": "Введите PIN-код ключа безопасности FIDO2:",
        "pin_guide": "👉 После ввода PIN и нажатия OK: КОСНИТЕСЬ КЛЮЧА YUBIKEY, когда он замигает!\n⚠️ При регистрации ключа потребуется коснуться ДВАЖДЫ (2 раза подряд).",
        "pin_layout": "🌐 Раскладка: %s",
        "pin_caps": "⚠️ ВКЛЮЧЁН CAPS LOCK!",
        "pin_show": "Показать PIN-код",
        "pin_ok": "OK (Подтвердить PIN)",
        "banner_enter_pin_title": "🔑 ВВЕДИТЕ PIN В ОКНЕ (а затем коснитесь ключа)",
        "banner_enter_pin_detail": "Введите PIN в появившемся окне и нажмите OK. После этого YubiKey замигает для касания.",
        "banner_touch_now_title": "👉 КОСНИТЕСЬ YUBIKEY СЕЙЧАС! (Устройство мигает)",
        "banner_touch_now_detail": "Прикоснитесь к золотому контакту ключа для подтверждения операции.",
        "banner_touch_twice_title": "👉 КОСНИТЕСЬ YUBIKEY ДВАЖДЫ (2x)!",
        "banner_touch_twice_detail": "1. Первое касание: регистрация учётных данных FIDO2\n2. Второе касание: генерация ключа BitLocker\nКлюч замигает два раза подряд. Коснитесь в оба момента!",
        "banner_lock_title": "🔒 ЗАБЛОКИРУЙТЕ WINDOWS (Нажмите Win + L)!",
        "banner_lock_detail": "Заблокируйте экран (Win+L), подождите 3-5 секунд и войдите снова.",
        "banner_container_title": "📦 Создание и настройка BitLocker VHDX контейнера...",
        "banner_container_detail": "Обычно занимает 10-20 секунд. Пожалуйста, подождите...",
        "banner_waiting": "⏳ Пожалуйста, подождите...",
        "banner_operation": "⏳ Выполняется операция... Пожалуйста, подождите",
        "step_dialog_title": "Secure Apps - Требуется действие",
        "step_dialog_head": "ТРЕБУЕТСЯ ДЕЙСТВИЕ / ACTION REQUIRED",
        "step_dialog_note": "Тест приостановлен для физического действия с ключом безопасности.\nВаш реальный vault Obsidian в безопасности и не затрагивается.",
        "rec_title": "Secure Apps - Данные восстановления BitLocker",
        "rec_head": "Данные восстановления BitLocker для '%s'",
        "rec_warn": "Сохраните этот пароль восстановления BitLocker оффлайн.\n\nОн позволит восстановить данные, если все зарегистрированные ключи YubiKey утеряны.\n\nSAITULS не хранит этот пароль.",
        "rec_tab_num": "Числовой ключ (48 цифр)",
        "rec_tab_mnemonic": "Мнемоническая фраза (12 слов)",
        "rec_tab_custom": "Свой мастер-пароль (6-12 слов)",
        "rec_copy_words_30s": "Скопировать 12 слов на 30 секунд",
        "rec_copied_title": "Скопировано на 30 секунд",
        "rec_copied_desc": "Данные восстановления скопированы в буфер обмена и будут автоматически очищены через 30 секунд.\n\nДве особенности буфера обмена:\n  - История буфера Windows (Win+V) сохраняет текст и может синхронизировать его с другими устройствами.\n  - Облачный буфер может передать ключ в сеть.\n\nЕсли эти функции активны, предпочтительнее записать ключ вручную. Оставлять его в буфере небезопасно.",
        "rec_mnemonic_desc": "Эта фраза из 12 слов математически эквивалентна 48-значному ключу BitLocker.\nВы можете записать эти 12 слов вместо 48 цифр.",
        "rec_mnemonic_na": "Мнемоника из 12 слов доступна для стандартных ключей BitLocker.",
        "rec_custom_prompt": "Опционально: введите свою фразу восстановления (от 6 до 12 слов):",
        "rec_custom_hint": "6-12 слов создают удобный дополнительный оффлайн-метод восстановления.",
    },
    "et": {
        "pin_title": "Secure Apps - Turvavõtme PIN",
        "pin_prompt": "Sisesta FIDO2 turvavõtme PIN-kood:",
        "pin_guide": "👉 Pärast PIN-koodi sisestamist ja OK vajutamist PUUDUTA YUBIKEYD kui see vilgub!\n⚠️ Registreerimisel puuduta KAKS KORDA (2x järjest).",
        "pin_layout": "🌐 Klaviatuur: %s",
        "pin_caps": "⚠️ CAPS LOCK ON SEES!",
        "pin_show": "Näita PIN-koodi",
        "pin_ok": "OK (Kinnita PIN)",
        "banner_enter_pin_title": "🔑 SISESTA PIN AKNAS (seejärel puuduta võtit)",
        "banner_enter_pin_detail": "Sisesta PIN avanenud aknas ja vajuta OK. Seejärel YubiKey hakkab vilkuma kinnituseks.",
        "banner_touch_now_title": "👉 PUUDUTA YUBIKEYD KOHE! (Seade vilgub)",
        "banner_touch_now_detail": "Puuduta seadme kuldset kontakti toimingu kinnitamiseks.",
        "banner_touch_twice_title": "👉 PUUDUTA YUBIKEYD KAKS KORDA (2x)!",
        "banner_touch_twice_detail": "1. puudutus: mandaadi loomine\n2. puudutus: BitLocker võtme sidumine\nSeade vilgub kaks korda järjest. Puuduta mõlemal korral!",
        "banner_lock_title": "🔒 LUKUSTA ARVUTI (Vajuta Win + L)!",
        "banner_lock_detail": "Lukusta Windows (Win+L), oota mõni sekund ja logi uuesti sisse.",
        "banner_container_title": "📦 BitLocker VHDX konteineri loomine...",
        "banner_container_detail": "See võtab tavaliselt umbes 10-20 sekundit. Palun oota...",
        "banner_waiting": "⏳ Palun oota...",
        "banner_operation": "⏳ Toiming käib... Palun oota",
        "step_dialog_title": "Secure Apps - Vajalik füüsiline toiming",
        "step_dialog_head": "VAJALIK TOIMING",
        "step_dialog_note": "Test peatus füüsilise toimingu ootel turvavõtmega.\nTeie päris Obsidian vault on puutumata.",
        "rec_title": "Secure Apps - BitLocker Taastematerjal",
        "rec_head": "BitLocker taastematerjal profiilile '%s'",
        "rec_warn": "Salvesta see BitLocker taasteparool eraldi.\n\nSee taastab andmed, kui kõik registreeritud YubiKey võtmed on kadunud.\n\nSAITULS ei salvesta seda parooli.",
        "rec_tab_num": "Numbriline võti (48 numbrit)",
        "rec_tab_mnemonic": "12-sõnaline mnemofraas",
        "rec_tab_custom": "Oma taastefraas (6-12 sõna)",
        "rec_copy_words_30s": "Kopeeri 12 sõna 30 sekundiks",
        "rec_copied_title": "Kopeeritud 30 sekundiks",
        "rec_copied_desc": "Taastematerjal on lõikelaual ja kustutatakse automaatselt 30 sekundi pärast.",
        "rec_mnemonic_desc": "See 12-sõnaline fraas vastab matemaatiliselt 48-numbrilisele BitLocker võtmele.\nVõite 48 numbri asemel üles kirjutada need 12 sõna.",
        "rec_mnemonic_na": "12-sõnaline fraas on saadaval standardsetele BitLocker võtmetele.",
        "rec_custom_prompt": "Valikuline: sisesta oma 6 kuni 12 sõnaline taastefraas:",
        "rec_custom_hint": "6-12 sõna pakub täiendavat inimloetavat taastevõimalust.",
    }
}


def tr(key):
    lang = get_system_ui_language()
    return STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))


# ══════════════════════════════════════════════════════════ layout & localization helpers
def get_current_keyboard_layout():
    """Detect current Windows keyboard layout and return a friendly string."""
    try:
        user32 = ctypes.windll.user32
        hkl = user32.GetKeyboardLayout(0)
        lid = hkl & 0xFFFF
        names = {
            0x0409: "EN (US English)",
            0x0809: "EN (UK English)",
            0x0419: "RU (Русский)",
            0x0425: "ET (Eesti)",
            0x0407: "DE (Deutsch)",
            0x040C: "FR (Français)",
            0x0410: "IT (Italiano)",
            0x040A: "ES (Español)",
            0x0422: "UK (Українська)",
        }
        return names.get(lid, "0x%04X" % lid)
    except Exception:
        return "Unknown"


def is_caps_lock_on():
    """Detect whether Caps Lock is currently toggled on."""
    try:
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)
    except Exception:
        return False


def get_system_ui_language():
    """Detect Windows OS UI display language: 'ru', 'et', or 'en'."""
    try:
        if os.name == "nt":
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            if lang_id == 0x19:
                return "ru"
            if lang_id == 0x25:
                return "et"
    except Exception:
        pass
    try:
        from PyQt6.QtCore import QLocale
        lang = QLocale.system().language()
        if lang == QLocale.Language.Russian:
            return "ru"
        if lang == QLocale.Language.Estonian:
            return "et"
    except Exception:
        pass
    return "en"


STRINGS = {
    "en": {
        "pin_title": "Secure Apps - Security Key PIN",
        "pin_prompt": "Enter FIDO2 Security Key PIN:",
        "pin_guide": "👉 After entering PIN and clicking OK: TOUCH YOUR YUBIKEY when it flashes!\n⚠️ During key registration, touch TWICE (2x in a row).",
        "pin_layout": "🌐 Layout: %s",
        "pin_caps": "⚠️ CAPS LOCK IS ON!",
        "pin_show": "Show PIN",
        "pin_ok": "OK (Confirm PIN)",
        "banner_enter_pin_title": "🔑 ENTER PIN IN DIALOG (then touch key)",
        "banner_enter_pin_detail": "Enter your FIDO2 PIN in the dialog and click OK. The YubiKey will then flash for touch confirmation.",
        "banner_touch_now_title": "👉 TOUCH YUBIKEY NOW! (Device is blinking)",
        "banner_touch_now_detail": "Touch the gold contact on the key to authorize the operation.",
        "banner_touch_twice_title": "👉 TOUCH YUBIKEY TWICE (2x)!",
        "banner_touch_twice_detail": "1. First touch: create credential (FIDO2)\n2. Second touch: bind BitLocker key\nTouch the key both times it flashes!",
        "banner_lock_title": "🔒 LOCK WINDOWS (Press Win + L)!",
        "banner_lock_detail": "Lock Windows (Win+L), wait 3-5 seconds, then log back in.",
        "banner_container_title": "📦 Preparing BitLocker VHDX container...",
        "banner_container_detail": "This usually takes 10-20 seconds. Please wait...",
        "banner_waiting": "⏳ Please wait...",
        "banner_operation": "⏳ Operation in progress... Please wait",
        "step_dialog_title": "Secure Apps - Physical Step Required",
        "step_dialog_head": "PHYSICAL ACTION REQUIRED",
        "step_dialog_note": "The test paused for a physical security key action (unplug/plug/lock).\nYour real Obsidian vault is safe and untouched.",
        "rec_title": "Secure Apps - BitLocker Recovery Material",
        "rec_head": "BitLocker recovery material for '%s'",
        "rec_warn": "Save this BitLocker recovery password offline.\n\nIt can recover your data if every enrolled YubiKey is lost.\n\nSAITULS does not store this password.",
        "rec_tab_num": "Numeric Key (48 digits)",
        "rec_tab_mnemonic": "12-Word Mnemonic Phrase",
        "rec_tab_custom": "Custom Passphrase (6-12 Words)",
        "rec_copy_words_30s": "Copy 12 Words for 30 seconds",
        "rec_copied_title": "Copied for 30 seconds",
        "rec_copied_desc": "The recovery material is on the clipboard and will be cleared automatically after 30 seconds.\n\nTwo things a clipboard does that paper does not:\n  - Windows clipboard history (Win+V) keeps what you copy, and can sync it to other signed-in devices.\n  - Cloud clipboard features may upload it off this machine.\n\nIf either is on, prefer typing the key by hand, or write it down. Leaving it on the clipboard indefinitely is not safe.",
        "rec_mnemonic_desc": "This 12-word phrase is mathematically equivalent to the 48-digit BitLocker key.\nYou can write down these 12 words instead of the 48 digits.",
        "rec_mnemonic_na": "12-word mnemonic is available for standard BitLocker keys.",
        "rec_custom_prompt": "Optional: Enter your own 6 to 12 words master recovery passphrase:",
        "rec_custom_hint": "6-12 words provide an extra human-readable offline recovery option.",
    },
    "ru": {
        "pin_title": "Secure Apps - PIN-код ключа FIDO2",
        "pin_prompt": "Введите PIN-код ключа безопасности FIDO2:",
        "pin_guide": "👉 После ввода PIN и нажатия OK: КОСНИТЕСЬ КЛЮЧА YUBIKEY, когда он замигает!\n⚠️ При регистрации ключа потребуется коснуться ДВАЖДЫ (2 раза подряд).",
        "pin_layout": "🌐 Раскладка: %s",
        "pin_caps": "⚠️ ВКЛЮЧЁН CAPS LOCK!",
        "pin_show": "Показать PIN-код",
        "pin_ok": "OK (Подтвердить PIN)",
        "banner_enter_pin_title": "🔑 ВВЕДИТЕ PIN В ОКНЕ (а затем коснитесь ключа)",
        "banner_enter_pin_detail": "Введите PIN в появившемся окне и нажмите OK. После этого YubiKey замигает для касания.",
        "banner_touch_now_title": "👉 КОСНИТЕСЬ YUBIKEY СЕЙЧАС! (Устройство мигает)",
        "banner_touch_now_detail": "Прикоснитесь к золотому контакту ключа для подтверждения операции.",
        "banner_touch_twice_title": "👉 КОСНИТЕСЬ YUBIKEY ДВАЖДЫ (2x)!",
        "banner_touch_twice_detail": "1. Первое касание: регистрация учётных данных FIDO2\n2. Второе касание: генерация ключа BitLocker\nКлюч замигает два раза подряд. Коснитесь в оба момента!",
        "banner_lock_title": "🔒 ЗАБЛОКИРУЙТЕ WINDOWS (Нажмите Win + L)!",
        "banner_lock_detail": "Заблокируйте экран (Win+L), подождите 3-5 секунд и войдите снова.",
        "banner_container_title": "📦 Создание и настройка BitLocker VHDX контейнера...",
        "banner_container_detail": "Обычно занимает 10-20 секунд. Пожалуйста, подождите...",
        "banner_waiting": "⏳ Пожалуйста, подождите...",
        "banner_operation": "⏳ Выполняется операция... Пожалуйста, подождите",
        "step_dialog_title": "Secure Apps - Требуется действие",
        "step_dialog_head": "ТРЕБУЕТСЯ ДЕЙСТВИЕ / ACTION REQUIRED",
        "step_dialog_note": "Тест приостановлен для физического действия с ключом безопасности.\nВаш реальный vault Obsidian в безопасности и не затрагивается.",
        "rec_title": "Secure Apps - Данные восстановления BitLocker",
        "rec_head": "Данные восстановления BitLocker для '%s'",
        "rec_warn": "Сохраните этот пароль восстановления BitLocker оффлайн.\n\nОн позволит восстановить данные, если все зарегистрированные ключи YubiKey утеряны.\n\nSAITULS не хранит этот пароль.",
        "rec_tab_num": "Числовой ключ (48 цифр)",
        "rec_tab_mnemonic": "Мнемоническая фраза (12 слов)",
        "rec_tab_custom": "Свой мастер-пароль (6-12 слов)",
        "rec_copy_words_30s": "Скопировать 12 слов на 30 секунд",
        "rec_copied_title": "Скопировано на 30 секунд",
        "rec_copied_desc": "Данные восстановления скопированы в буфер обмена и будут автоматически очищены через 30 секунд.\n\nДве особенности буфера обмена:\n  - История буфера Windows (Win+V) сохраняет текст и может синхронизировать его с другими устройствами.\n  - Облачный буфер может передать ключ в сеть.\n\nЕсли эти функции активны, предпочтительнее записать ключ вручную. Оставлять его в буфере небезопасно.",
        "rec_mnemonic_desc": "Эта фраза из 12 слов математически эквивалентна 48-значному ключу BitLocker.\nВы можете записать эти 12 слов вместо 48 цифр.",
        "rec_mnemonic_na": "Мнемоника из 12 слов доступна для стандартных ключей BitLocker.",
        "rec_custom_prompt": "Опционально: введите свою фразу восстановления (от 6 до 12 слов):",
        "rec_custom_hint": "6-12 слов создают удобный дополнительный оффлайн-метод восстановления.",
    },
    "et": {
        "pin_title": "Secure Apps - Turvavõtme PIN",
        "pin_prompt": "Sisesta FIDO2 turvavõtme PIN-kood:",
        "pin_guide": "👉 Pärast PIN-koodi sisestamist ja OK vajutamist PUUDUTA YUBIKEYD kui see vilgub!\n⚠️ Registreerimisel puuduta KAKS KORDA (2x järjest).",
        "pin_layout": "🌐 Klaviatuur: %s",
        "pin_caps": "⚠️ CAPS LOCK ON SEES!",
        "pin_show": "Näita PIN-koodi",
        "pin_ok": "OK (Kinnita PIN)",
        "banner_enter_pin_title": "🔑 SISESTA PIN AKNAS (seejärel puuduta võtit)",
        "banner_enter_pin_detail": "Sisesta PIN avanenud aknas ja vajuta OK. Seejärel YubiKey hakkab vilkuma kinnituseks.",
        "banner_touch_now_title": "👉 PUUDUTA YUBIKEYD KOHE! (Seade vilgub)",
        "banner_touch_now_detail": "Puuduta seadme kuldset kontakti toimingu kinnitamiseks.",
        "banner_touch_twice_title": "👉 PUUDUTA YUBIKEYD KAKS KORDA (2x)!",
        "banner_touch_twice_detail": "1. puudutus: mandaadi loomine\n2. puudutus: BitLocker võtme sidumine\nSeade vilgub kaks korda järjest. Puuduta mõlemal korral!",
        "banner_lock_title": "🔒 LUKUSTA ARVUTI (Vajuta Win + L)!",
        "banner_lock_detail": "Lukusta Windows (Win+L), oota mõni sekund ja logi uuesti sisse.",
        "banner_container_title": "📦 BitLocker VHDX konteineri loomine...",
        "banner_container_detail": "See võtab tavaliselt umbes 10-20 sekundit. Palun oota...",
        "banner_waiting": "⏳ Palun oota...",
        "banner_operation": "⏳ Toiming käib... Palun oota",
        "step_dialog_title": "Secure Apps - Vajalik füüsiline toiming",
        "step_dialog_head": "VAJALIK TOIMING",
        "step_dialog_note": "Test peatus füüsilise toimingu ootel turvavõtmega.\nTeie päris Obsidian vault on puutumata.",
        "rec_title": "Secure Apps - BitLocker Taastematerjal",
        "rec_head": "BitLocker taastematerjal profiilile '%s'",
        "rec_warn": "Salvesta see BitLocker taasteparool eraldi.\n\nSee taastab andmed, kui kõik registreeritud YubiKey võtmed on kadunud.\n\nSAITULS ei salvesta seda parooli.",
        "rec_tab_num": "Numbriline võti (48 numbrit)",
        "rec_tab_mnemonic": "12-sõnaline mnemofraas",
        "rec_tab_custom": "Oma taastefraas (6-12 sõna)",
        "rec_copy_words_30s": "Kopeeri 12 sõna 30 sekundiks",
        "rec_copied_title": "Kopeeritud 30 sekundiks",
        "rec_copied_desc": "Taastematerjal on lõikelaual ja kustutatakse automaatselt 30 sekundi pärast.",
        "rec_mnemonic_desc": "See 12-sõnaline fraas vastab matemaatiliselt 48-numbrilisele BitLocker võtmele.\nVõite 48 numbri asemel üles kirjutada need 12 sõna.",
        "rec_mnemonic_na": "12-sõnaline fraas on saadaval standardsetele BitLocker võtmetele.",
        "rec_custom_prompt": "Valikuline: sisesta oma 6 kuni 12 sõnaline taastefraas:",
        "rec_custom_hint": "6-12 sõna pakub täiendavat inimloetavat taastevõimalust.",
    }
}


def tr(key):
    lang = get_system_ui_language()
    return STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))


# ══════════════════════════════════════════════════════════ dialogs
class RecoveryDialog(QDialog):
    """Shows the BitLocker recovery material once and demands proof of capture.

    Typing a fixed phrase proves nothing: it is in the source code, so anyone
    reading this file -- or anyone who watched the previous run -- knows it and
    can type it without ever having written the key down. The check that
    actually means something is asking for parts of the key itself, chosen
    unpredictably at the moment of the ask.
    """

    CONFIRM = "I HAVE STORED IT"
    #: How many groups of the key must be reproduced. Two is enough that a
    #: remembered-from-a-screenshot guess is not the easy path, and few enough
    #: that transcription fatigue cannot lock the owner out of their own vault.
    CHALLENGE_GROUPS = 2
    #: How long the recovery password may sit on the clipboard after an
    #: explicit "copy". Long enough to paste into a password manager, short
    #: enough that walking away from the machine does not leave it there.
    CLIPBOARD_LIFETIME_MS = 30_000

    def __init__(self, profile_id, recovery, parent=None):
        QDialog.__init__(self, parent)
        self.recovery_text = recovery
        self.mnemonic_phrase = sa_mnemonic.bitlocker_to_mnemonic(recovery) or ""
        self.setWindowTitle(tr("rec_title"))
        self.setMinimumWidth(640)
        layout = QVBoxLayout(self)

        head = QLabel(tr("rec_head") % profile_id)
        head.setObjectName("title")
        layout.addWidget(head)

        warn = QLabel(tr("rec_warn"))
        warn.setObjectName("danger")
        layout.addWidget(warn)

        self.tabs = QTabWidget()

        # Tab 1: Numeric Key
        tab_num = QWidget()
        tab_num_layout = QVBoxLayout(tab_num)
        self.text = QPlainTextEdit(recovery)
        self.text.setReadOnly(True)
        self.text.setFixedHeight(80)
        tab_num_layout.addWidget(self.text)

        btn_row = QHBoxLayout()
        btn_copy = QPushButton("Copy for 30 seconds")
        btn_print = QPushButton("Print / Save instructions")
        btn_row.addWidget(btn_copy)
        btn_row.addWidget(btn_print)
        btn_row.addStretch()
        tab_num_layout.addLayout(btn_row)
        self.tabs.addTab(tab_num, tr("rec_tab_num"))

        btn_copy.clicked.connect(self._copy)
        btn_print.clicked.connect(self._instructions)

        # Tab 2: 12-Word Mnemonic Phrase
        tab_words = QWidget()
        tab_words_layout = QVBoxLayout(tab_words)
        if self.mnemonic_phrase:
            words_list = self.mnemonic_phrase.split()
            words_formatted = "  ".join("%d. %s" % (i + 1, w) for i, w in enumerate(words_list))
            self.words_text = QPlainTextEdit(words_formatted)
            self.words_text.setReadOnly(True)
            self.words_text.setFixedHeight(80)
            tab_words_layout.addWidget(self.words_text)

            btn_words_row = QHBoxLayout()
            btn_copy_words = QPushButton(tr("rec_copy_words_30s"))
            btn_words_row.addWidget(btn_copy_words)
            btn_words_row.addStretch()
            tab_words_layout.addLayout(btn_words_row)
            btn_copy_words.clicked.connect(self._copy_words)
            tab_words_layout.addWidget(QLabel(tr("rec_mnemonic_desc")))
        else:
            tab_words_layout.addWidget(QLabel(tr("rec_mnemonic_na")))
        self.tabs.addTab(tab_words, tr("rec_tab_mnemonic"))

        layout.addWidget(self.tabs)

        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.setSingleShot(True)
        self._clipboard_timer.setInterval(self.CLIPBOARD_LIFETIME_MS)
        self._clipboard_timer.timeout.connect(self._clear_clipboard)

        layout.addWidget(QLabel(
            "Typing the words below is the strongest way to capture this key: "
            "it never touches the clipboard, the clipboard history, or any sync "
            "service. "))
        layout.addWidget(QLabel("Type  %s  to continue (anything else aborts):" % self.CONFIRM))
        self.entry = QLineEdit()
        layout.addWidget(self.entry)

        # The challenge. Two groups of the key, picked at random each time this
        # dialog opens, typed from whatever offline copy the operator has.
        self.challenge_boxes = []
        self._challenge_indices = []
        challenge_row = QHBoxLayout()
        for position in range(self.CHALLENGE_GROUPS):
            label = QLabel("Group %d:" % (position + 1))
            box = QLineEdit()
            box.setMaxLength(16)
            box.setEchoMode(QLineEdit.EchoMode.Password)
            challenge_row.addWidget(label)
            challenge_row.addWidget(box)
            self.challenge_boxes.append(box)
        challenge_row.addStretch()
        self.challenge_hint = QLabel("")
        self.challenge_hint.setWordWrap(True)
        layout.addWidget(QLabel(
            "Then type %d group(s) of the key below, taken from your offline "
            "copy -- not from the window above:" % self.CHALLENGE_GROUPS))
        layout.addLayout(challenge_row)
        layout.addWidget(self.challenge_hint)
        self._new_challenge()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)
        self.entry.textChanged.connect(
            lambda t: self._ok.setEnabled(t.strip().upper() == self.CONFIRM))

    # -- the challenge ----------------------------------------------------
    def _key_groups(self):
        return [group for group in self.recovery_text.replace("-", " ").split()
                if group]

    def _new_challenge(self):
        """Pick fresh groups for the next attempt."""
        groups = self._key_groups()
        if len(groups) < 2:
            self._challenge_indices = []
            self.challenge_hint.setText("")
            return
        count = min(self.CHALLENGE_GROUPS, len(groups))
        self._challenge_indices = sorted(
            secrets.SystemRandom().sample(range(len(groups)), count))
        idx_str = ", ".join(str(i + 1) for i in self._challenge_indices)
        words = self.__dict__.get("mnemonic_phrase", "").split()
        if words and len(words) == len(groups):
            self.challenge_hint.setText(
                "Groups/words %s of the key (the key has %d groups / 12 words)."
                % (idx_str, len(groups)))
        else:
            self.challenge_hint.setText(
                "Groups %s of the key (the key has %d groups)."
                % (idx_str, len(groups)))
        self._clear_challenge_inputs()

    def _clear_challenge_inputs(self):
        for box in self.challenge_boxes:
            box.clear()

    def _challenge_answered(self):
        """Constant-time-ish comparison; accepts numeric group OR mnemonic word."""
        groups = self._key_groups()
        words = self.__dict__.get("mnemonic_phrase", "").split()
        if not self._challenge_indices:
            return True
        typed = [box.text().strip().lower() for box in self.challenge_boxes]
        self._clear_challenge_inputs()
        if len(typed) != len(self._challenge_indices):
            return False
        ok = True
        for typed_value, index in zip(typed, self._challenge_indices):
            expected_num = groups[index].lower()
            expected_word = words[index].lower() if index < len(words) else ""
            match_num = secrets.compare_digest(typed_value, expected_num)
            match_word = secrets.compare_digest(typed_value, expected_word) if expected_word else False
            ok &= (match_num or match_word)
        return bool(ok)

    # -- clipboard hygiene ------------------------------------------------
    def _clear_clipboard(self):
        """Empty the clipboard, but only if it still holds this key or mnemonic."""
        clipboard = QApplication.clipboard()
        current = clipboard.text()
        recovery = self.__dict__.get("recovery_text", "")
        mnemonic = self.__dict__.get("mnemonic_phrase", "")
        if current != recovery and current != mnemonic:
            return False
        clipboard.clear()
        return True

    def _copy(self):
        QApplication.clipboard().setText(self.recovery_text)
        self._clipboard_timer.start()
        QMessageBox.information(
            self, tr("rec_copied_title"),
            tr("rec_copied_desc"))

    def _copy_words(self):
        mnemonic = self.__dict__.get("mnemonic_phrase", "")
        if not mnemonic:
            return
        QApplication.clipboard().setText(mnemonic)
        self._clipboard_timer.start()
        QMessageBox.information(
            self, tr("rec_copied_title"),
            tr("rec_copied_desc"))

    def closeEvent(self, event):
        self._clipboard_timer.stop()
        self._clear_clipboard()
        QDialog.closeEvent(self, event)

    def _instructions(self):
        QMessageBox.information(
            self, "Recovery Instructions",
            "HOW TO STORE YOUR RECOVERY PASSWORD SAFELY:\n\n"
            "1. Write down the 48-digit key or the 12-word mnemonic on paper and store it in a physical safe.\n"
            "2. Or store it in an offline password manager (KeePass, 1Password, Bitwarden).\n"
            "3. DO NOT save it as a plaintext file on this machine.\n"
            "4. DO NOT take a screenshot or send it over network.\n\n"
            "If all YubiKeys are lost, you can use Windows Disk Management and BitLocker "
            "to unlock the VHDX container with this key.")

    def _accept(self):
        if self.entry.text().strip().upper() != self.CONFIRM:
            return
        if not self._challenge_answered():
            self._new_challenge()
            QMessageBox.warning(
                self, "Proof not accepted",
                "One or more groups/words did not match.\n\n"
                "Check the groups or words you were asked for against your written or "
                "offline-stored key. Nothing was saved, and nothing is lost by "
                "looking again -- the vault is created either way.")
            return
        self.entry.clear()
        self.accept()


class PortablePasswordEnrollDialog(QDialog):
    """Enables setting or generating the native BitLocker Portable Master Password."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Portable Master Password")
        self.setMinimumWidth(580)
        self.password = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        head = QLabel("PORTABLE MASTER PASSWORD")
        head.setObjectName("title")
        head.setStyleSheet("font-size: 11pt; font-weight: bold; color: #FFE066;")
        layout.addWidget(head)

        info = QLabel(
            "This password is a native Windows BitLocker password protector on the vault.\n"
            "It allows opening and recovering your files on ANY Windows 10/11 machine\n"
            "without SAITULS, YubiKey, or special software installed."
        )
        info.setObjectName("sub")
        info.setWordWrap(True)
        layout.addWidget(info)

        btn_row = QHBoxLayout()
        self.btn_gen = QPushButton("Generate Strong Passphrase (9 BIP-39 words)")
        self.btn_gen.setObjectName("primary_action")
        btn_row.addWidget(self.btn_gen)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("Portable Password or Passphrase:"))
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Enter or generate high-entropy passphrase")
        layout.addWidget(self.entry)

        self.lbl_feedback = QLabel("")
        self.lbl_feedback.setWordWrap(True)
        layout.addWidget(self.lbl_feedback)

        layout.addWidget(QLabel("Confirm Password (re-type or paste):"))
        self.confirm_entry = QLineEdit()
        self.confirm_entry.setPlaceholderText("Confirm to prove capture")
        layout.addWidget(self.confirm_entry)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.btn_ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.btn_ok.setEnabled(False)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.btn_gen.clicked.connect(self._generate)
        self.entry.textChanged.connect(self._validate)
        self.confirm_entry.textChanged.connect(self._validate)

        self._generate()

    def _generate(self):
        phrase = sa_mnemonic.generate_portable_passphrase(9)
        self.entry.setText(phrase)
        self.confirm_entry.clear()
        self.lbl_feedback.setText("Generated 9-word phrase (~99 bits entropy). Record it offline!")
        self.lbl_feedback.setStyleSheet("color: #70D080;")
        self._validate()

    def _validate(self):
        pw = self.entry.text().strip()
        conf = self.confirm_entry.text().strip()
        ok, msg = sa_mnemonic.validate_master_password(pw)
        if not ok:
            self.lbl_feedback.setText(msg)
            self.lbl_feedback.setStyleSheet("color: #D66464;")
            self.btn_ok.setEnabled(False)
            return
        if pw != conf:
            self.lbl_feedback.setText("Passwords do not match yet.")
            self.lbl_feedback.setStyleSheet("color: #E0C068;")
            self.btn_ok.setEnabled(False)
            return

        self.lbl_feedback.setText("Password accepted and verified.")
        self.lbl_feedback.setStyleSheet("color: #70D080;")
        self.btn_ok.setEnabled(True)

    def _on_accept(self):
        self.password = self.entry.text().strip()
        self.accept()


class ChangePortablePasswordDialog(QDialog):
    """Allows updating or adding a native BitLocker Portable Master Password."""

    def __init__(self, mode="change", parent=None):
        super().__init__(parent)
        self.mode = mode
        self.setWindowTitle("Change Portable Password" if mode == "change" else "Add Portable Password")
        self.setMinimumWidth(560)
        self.new_password = None
        self.old_password = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        head = QLabel("UPDATE PORTABLE MASTER PASSWORD" if mode == "change" else "ADD PORTABLE MASTER PASSWORD")
        head.setObjectName("title")
        head.setStyleSheet("font-size: 11pt; font-weight: bold; color: #FFE066;")
        layout.addWidget(head)

        if mode == "change":
            layout.addWidget(QLabel("Current Portable Password (for verified atomic rollback):"))
            self.current_entry = QLineEdit()
            self.current_entry.setEchoMode(QLineEdit.EchoMode.Password)
            layout.addWidget(self.current_entry)
        else:
            self.current_entry = None

        btn_row = QHBoxLayout()
        self.btn_gen = QPushButton("Generate Strong Passphrase (9 BIP-39 words)")
        btn_row.addWidget(self.btn_gen)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        layout.addWidget(QLabel("New Portable Password / Passphrase:"))
        self.entry = QLineEdit()
        layout.addWidget(self.entry)

        self.lbl_feedback = QLabel("")
        self.lbl_feedback.setWordWrap(True)
        layout.addWidget(self.lbl_feedback)

        layout.addWidget(QLabel("Confirm New Password:"))
        self.confirm_entry = QLineEdit()
        layout.addWidget(self.confirm_entry)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.btn_ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.btn_ok.setEnabled(False)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.btn_gen.clicked.connect(self._generate)
        self.entry.textChanged.connect(self._validate)
        self.confirm_entry.textChanged.connect(self._validate)
        self._generate()

    def _generate(self):
        phrase = sa_mnemonic.generate_portable_passphrase(9)
        self.entry.setText(phrase)
        self.confirm_entry.clear()
        self._validate()

    def _validate(self):
        pw = self.entry.text().strip()
        conf = self.confirm_entry.text().strip()
        ok, msg = sa_mnemonic.validate_master_password(pw)
        if not ok:
            self.lbl_feedback.setText(msg)
            self.lbl_feedback.setStyleSheet("color: #D66464;")
            self.btn_ok.setEnabled(False)
            return
        if pw != conf:
            self.lbl_feedback.setText("Passwords do not match yet.")
            self.lbl_feedback.setStyleSheet("color: #E0C068;")
            self.btn_ok.setEnabled(False)
            return

        self.lbl_feedback.setText("Password accepted.")
        self.lbl_feedback.setStyleSheet("color: #70D080;")
        self.btn_ok.setEnabled(True)

    def _on_accept(self):
        self.new_password = self.entry.text().strip()
        if self.current_entry:
            self.old_password = self.current_entry.text().strip() or None
        self.accept()


class OpenPortableVaultDialog(QDialog):
    """Dialog to mount an arbitrary portable encrypted vault (.svault / .vhdx)."""

    def __init__(self, default_vault_path="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open Portable Vault")
        self.setMinimumWidth(600)
        self.result_data = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        head = QLabel("OPEN PORTABLE ENCRYPTED VAULT")
        head.setObjectName("title")
        head.setStyleSheet("font-size: 11pt; font-weight: bold; color: #FFE066;")
        layout.addWidget(head)

        layout.addWidget(QLabel("Vault File Path (.svault / .vhdx):"))
        v_row = QHBoxLayout()
        self.vault_entry = QLineEdit(default_vault_path)
        btn_browse_vault = QPushButton("Browse...")
        btn_browse_vault.clicked.connect(self._browse_vault)
        v_row.addWidget(self.vault_entry, 1)
        v_row.addWidget(btn_browse_vault)
        layout.addLayout(v_row)

        layout.addWidget(QLabel("Mount Target Directory (must be empty):"))
        m_row = QHBoxLayout()
        self.mount_entry = QLineEdit(os.path.join(os.environ.get("TEMP", r"C:\Temp"), "portable_mount"))
        btn_browse_mount = QPushButton("Browse...")
        btn_browse_mount.clicked.connect(self._browse_mount)
        m_row.addWidget(self.mount_entry, 1)
        m_row.addWidget(btn_browse_mount)
        layout.addLayout(m_row)

        layout.addWidget(QLabel("Unlock Method:"))
        method_row = QHBoxLayout()
        self.rb_pw = QRadioButton("Portable Password")
        self.rb_rec = QRadioButton("48-digit Recovery Key")
        self.rb_pw.setChecked(True)
        method_row.addWidget(self.rb_pw)
        method_row.addWidget(self.rb_rec)
        method_row.addStretch()
        layout.addLayout(method_row)

        layout.addWidget(QLabel("Password / Recovery Key:"))
        self.secret_entry = QLineEdit()
        self.secret_entry.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.secret_entry)

        self.cb_readonly = QCheckBox("Mount Read-Only (Safe forensic / inspection mode)")
        self.cb_readonly.setChecked(True)
        layout.addWidget(self.cb_readonly)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Unlock && Mount")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_vault(self):
        f, _ = QFileDialog.getOpenFileName(self, "Select Vault File", "", "Vault Files (*.svault *.vhdx);;All Files (*.*)")
        if f:
            self.vault_entry.setText(f)

    def _browse_mount(self):
        d = QFileDialog.getExistingDirectory(self, "Select Empty Mount Directory")
        if d:
            self.mount_entry.setText(d)

    def _on_accept(self):
        v = self.vault_entry.text().strip()
        m = self.mount_entry.text().strip()
        s = self.secret_entry.text().strip()
        if not v or not os.path.isfile(v):
            QMessageBox.warning(self, "Invalid Path", "Please select an existing vault file.")
            return
        if not m:
            QMessageBox.warning(self, "Invalid Path", "Please specify a mount directory.")
            return
        if not s:
            QMessageBox.warning(self, "Missing Secret", "Please enter the password or recovery key.")
            return
        self.result_data = {
            "vault_path": v,
            "mount_path": m,
            "is_recovery": self.rb_rec.isChecked(),
            "secret": s,
            "read_only": self.cb_readonly.isChecked(),
        }
        self.accept()


class PinDialog(QDialog):
    """Masked PIN entry for FIDO2 operations with real-time layout indicator and touch guidance."""

    def __init__(self, parent=None, prompt="Enter FIDO2 Security Key PIN:"):
        QDialog.__init__(self, parent)
        self.setWindowTitle(tr("pin_title"))
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        head = QLabel(prompt or tr("pin_prompt"))
        head.setObjectName("title")
        head.setStyleSheet("font-size: 11pt; font-weight: bold; color: #FFE066;")
        head.setWordWrap(True)
        layout.addWidget(head)

        guide = QLabel(tr("pin_guide"))
        guide.setStyleSheet(
            "background: #252010; color: #E0C068; padding: 8px; "
            "border: 1px solid #806020; border-radius: 4px; font-weight: bold; font-size: 10pt;"
        )
        guide.setWordWrap(True)
        layout.addWidget(guide)

        info_row = QHBoxLayout()
        current_lang = get_current_keyboard_layout()
        self.lbl_lang = QLabel(tr("pin_layout") % current_lang)
        self.lbl_lang.setStyleSheet("color: #70B0FF; font-weight: bold;")
        info_row.addWidget(self.lbl_lang)

        caps = is_caps_lock_on()
        self.lbl_caps = QLabel(tr("pin_caps"))
        self.lbl_caps.setStyleSheet("color: #FF5555; font-weight: bold;")
        self.lbl_caps.setVisible(caps)
        info_row.addWidget(self.lbl_caps)
        info_row.addStretch()
        layout.addLayout(info_row)

        self.entry = QLineEdit()
        self.entry.setEchoMode(QLineEdit.EchoMode.Password)
        self.entry.setMinimumHeight(32)
        self.entry.setStyleSheet("font-size: 12pt; padding: 4px;")
        layout.addWidget(self.entry)

        chk_show = QCheckBox(tr("pin_show"))
        chk_show.setStyleSheet("color: #CCCCCC; font-size: 10pt;")
        def toggle_echo(checked):
            self.entry.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        chk_show.toggled.connect(toggle_echo)
        layout.addWidget(chk_show)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText(tr("pin_ok"))
        self._ok.setEnabled(False)
        self.entry.textChanged.connect(lambda t: self._ok.setEnabled(len(t) >= 4))
        self.entry.setFocus()

        # Real-time keyboard layout and CapsLock detector timer (every 100ms)
        self._layout_timer = QTimer(self)
        self._layout_timer.setInterval(100)
        self._layout_timer.timeout.connect(self._refresh_layout_and_caps)
        self._layout_timer.start()

    def _refresh_layout_and_caps(self):
        cur = get_current_keyboard_layout()
        self.lbl_lang.setText(tr("pin_layout") % cur)
        caps = is_caps_lock_on()
        self.lbl_caps.setVisible(caps)

    def closeEvent(self, event):
        if hasattr(self, "_layout_timer"):
            self._layout_timer.stop()
        super().closeEvent(event)

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

        head = QLabel(tr("step_dialog_head"))
        head.setObjectName("title")
        head.setStyleSheet("font-size: 13pt; font-weight: bold; color: #FFD700;")
        layout.addWidget(head)

        note = QLabel(tr("step_dialog_note"))
        note.setObjectName("sub")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.prompt = QLabel(str(prompt_text))
        self.prompt.setWordWrap(True)
        self.prompt.setStyleSheet(
            "font-size: 13pt; font-weight: bold; color: #FFE066; background: #2A2410; "
            "border: 2px solid #FFC800; border-radius: 6px; padding: 12px;"
        )
        layout.addWidget(self.prompt)

        row = QHBoxLayout()
        self.btn_continue = QPushButton("Continue")
        self.btn_continue.setObjectName("primary_action")
        self.btn_continue.setMinimumHeight(34)
        self.btn_cancel = QPushButton("Cancel acceptance")
        self.btn_cancel.setObjectName("danger")
        self.btn_cancel.setMinimumHeight(34)
        row.addWidget(self.btn_continue)
        row.addWidget(self.btn_cancel)
        row.addStretch()
        layout.addLayout(row)

        self.btn_continue.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_continue.setDefault(True)
        self.btn_continue.setFocus()


class GrowTargetDialog(QDialog):
    """Ask for the one number a grow plan needs, in the product's own theme.

    Asks; does not act. The number is validated against the measured capacity
    before it is accepted, and nothing is resized by this dialog -- it only
    names a target for a read-only plan evaluation.
    """

    def __init__(self, measured_gb, provisioned_gb=None, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle("Secure Apps - Grow plan target")
        self.setMinimumWidth(460)
        self.target_gb = None
        self._measured = measured_gb
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        title = QLabel("PLAN A GROWTH (READ ONLY)")
        title.setObjectName("title")
        layout.addWidget(title)
        measured = "UNKNOWN" if measured_gb is None else "%.2f GiB" % measured_gb
        configured = ("unknown" if provisioned_gb is None
                      else "%s GiB" % provisioned_gb)
        info = QLabel(
            "Measured virtual capacity: %s\n"
            "Provisioned in secure_apps.json: %s\n\n"
            "The target must be a whole number of GiB larger than the measured \r\n"
            "capacity. Nothing is resized by this dialog: it evaluates a plan."
            % (measured, configured))
        info.setObjectName("sub")
        info.setWordWrap(True)
        layout.addWidget(info)
        row = QHBoxLayout()
        row.addWidget(QLabel("New size (GiB):"))
        default = 8 if measured_gb is None else int(measured_gb) * 2
        self.edit = QLineEdit(str(default))
        row.addWidget(self.edit)
        layout.addLayout(row)
        self.note = QLabel("")
        self.note.setObjectName("danger")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        raw = self.edit.text().strip()
        try:
            value = int(raw)
        except ValueError:
            self.note.setText("Enter a whole number of GiB.")
            return
        if value <= 0:
            self.note.setText("The target must be greater than zero GiB.")
            return
        if value > sa_capacity.MAX_GROW_GB:
            self.note.setText("The VHDX maximum is %d GiB."
                              % sa_capacity.MAX_GROW_GB)
            return
        if self._measured is not None and value <= self._measured:
            self.note.setText("The target must exceed the measured capacity "
                              "of %.2f GiB." % self._measured)
            return
        self.target_gb = value
        self.accept()


class CapacityReportDialog(QDialog):
    """A read-only, themed, scrollable text report (capacity facts or a plan)."""

    def __init__(self, title_text, body, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle(title_text)
        self.setMinimumSize(640, 460)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        title = QLabel(title_text.upper())
        title.setObjectName("title")
        layout.addWidget(title)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setPlainText(body)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.body, stretch=1)
        row = QHBoxLayout()
        self.btn_copy = QPushButton("Copy report")
        self.btn_close = QPushButton("Close")
        row.addWidget(self.btn_copy)
        row.addStretch()
        row.addWidget(self.btn_close)
        layout.addLayout(row)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_close.clicked.connect(self.accept)

    def _copy(self):
        QApplication.clipboard().setText(self.body.toPlainText())


def grow_plan_text(payload):
    """The plan exactly as measured, or the refusal and why, never both."""
    facts = payload.get("facts") or {}
    toolchain = payload.get("toolchain") or {}
    target = payload.get("new_gb")
    lines = []
    if payload.get("detail"):
        lines.append("The plan could not be evaluated: %s" % payload["detail"])
        lines.append("")
    plan = payload.get("plan")
    if plan:
        lines.append("RESULT: PLAN AVAILABLE (read-only preview)")
        lines.append("")
        lines.append("  %-26s %s GiB" % ("Target size:", plan["new_gb"]))
        lines.append("  %-26s %.2f GiB (measured)"
                     % ("Current capacity:", plan["measured_capacity_gb"]))
        lines.append("  %-26s %.2f GiB" % ("Growth delta:", plan["delta_gb"]))
        lines.append("  %-26s %s" % ("Host free:", gb_text(plan.get("host_free_gb"))))
        lines.append("")
        lines.append("THE EXACT PROPOSED CHANGE")
        lines.append("  %s" % plan["command"])
    else:
        lines.append("RESULT: REFUSED")
        lines.append("")
        lines.append("  %s" % (payload.get("refusal") or "unknown refusal"))
    lines.append("")
    lines.append("PRE-OPERATION REQUIREMENTS, IN ORDER")
    for index, step in enumerate(payload.get("procedure") or [], start=1):
        lines.append("  %d. [%s] %s" % (index, step["requires"], step["text"]))
    lines.append("")
    lines.append("PRECONDITIONS MEASURED NOW")
    for check in payload.get("checks") or []:
        lines.append("  [%s] %-32s %s" % ("OK " if check["ok"] else "NO ",
                                          check["code"], check["detail"]))
    if not payload.get("checks"):
        lines.append("  (not evaluated)")
    lines.append("")
    lines.append("EXPECTED POST-OPERATION CHECKS")
    lines.append("  - Re-read the VHDX VirtualSize and require it to equal the target.")
    lines.append("  - Confirm the internal filesystem was extended to fill the disk.")
    lines.append("  - Confirm BitLocker still reports protection ON.")
    lines.append("  - Confirm the vault still mounts and the protected app launches.")
    lines.append("")
    lines.append("Relevant measured facts")
    lines.append("  Container:        %s" % (facts.get("container") or "-"))
    lines.append("  Virtual capacity: %s" % gb_text(facts.get("virtual_capacity_gb")))
    lines.append("  On disk:          %s" % gb_text(facts.get("allocated_gb")))
    lines.append("  Host free:        %s" % gb_text(facts.get("host_free_gb")))
    lines.append("  Attached/loaded:  %s" % facts.get("loaded"))
    lines.append("  Resize-VHD:       %s" % ("available" if toolchain.get("available")
                                              else "NOT available (%s)"
                                              % (toolchain.get("detail") or "unknown")))
    lines.append("")
    if plan:
        lines.append("This window offers NO executable grow button. A plan that is")
        lines.append("available here still runs only through an explicitly consented,")
        lines.append("supervised operation; a printed plan is not a grow operation.")
    else:
        lines.append("Nothing is executable while the plan is refused: this dialog only")
        lines.append("prints requirements and the reason they are not met.")
    return "\n".join(lines)


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
# ── Capacity (T-256): verdicts and honest number formatting ─────────
#: Every verdict ``sa_capacity.classify`` can return, in the operator's words.
CAPACITY_VERDICT_TEXT = {
    "missing": "CONTAINER MISSING",
    "container_format_invalid": "NOT A VALID VHDX",
    "container_attached": "CONTAINER ATTACHED",
    "host_volume_unknown": "HOST VOLUME UNKNOWN",
    "host_free_unknown": "HOST FREE SPACE UNKNOWN",
    "grow_blocked_host_full": "HOST TOO FULL TO GROW",
    "host_space_warning": "HOST SPACE LOW",
    "host_space_ok": "HOST SPACE OK",
    "virtual_capacity_unknown": "VIRTUAL CAPACITY UNKNOWN",
    "container_over_capacity": "FILE LARGER THAN CAPACITY",
    "container_sparse": "DYNAMIC / SPARSE",
}


def gb_text(value, unknown="UNKNOWN"):
    """A measured number, or the word UNKNOWN -- never a fabricated zero."""
    if value is None:
        return unknown
    return "%.2f GiB" % value


def capacity_summary_line(facts):
    if facts.get("exists") is False:
        return "container not found: %s" % (facts.get("container") or "<no path>")
    parts = ["virtual %s" % gb_text(facts.get("virtual_capacity_gb")),
             "on disk %s" % gb_text(facts.get("allocated_gb"))]
    if facts.get("internal_free_gb") is None:
        parts.append("internal used/free UNKNOWN (needs an unlocked volume)")
    else:
        parts.append("internal used %s / free %s" % (
            gb_text(facts.get("internal_used_gb")),
            gb_text(facts.get("internal_free_gb"))))
    if facts.get("host_free_gb") is None:
        parts.append("host free UNKNOWN")
    else:
        parts.append("host free %s of %s on %s" % (
            gb_text(facts.get("host_free_gb")),
            gb_text(facts.get("host_total_gb")),
            facts.get("host_volume") or "unknown volume"))
    return " · ".join(parts)


def capacity_status_line(facts, toolchain):
    verdicts = [CAPACITY_VERDICT_TEXT.get(v, str(v).upper())
                for v in (facts.get("verdicts") or [])] or ["UNKNOWN"]
    toolchain = toolchain or {}
    if toolchain.get("available"):
        support = "growth supported (Resize-VHD present)"
    else:
        support = "growth NOT AVAILABLE (%s)" % (
            toolchain.get("detail") or "Resize-VHD missing")
    return ("%s  |  warn below %.0f GiB free or %.0f%% host used, refuse below "
            "%.0f GiB free  |  %s  |  measured %s") % (
        ", ".join(verdicts), sa_capacity.WARN_MIN_FREE_GB,
        sa_capacity.WARN_USED_RATIO * 100.0, sa_capacity.GROW_MIN_FREE_GB,
        support, facts.get("measured_at") or "never")


def capacity_report_text(facts, toolchain, verdicts):
    """The full breakdown, with the source of every single number."""
    lines = ["MEASURED CAPACITY FACTS"]

    def line(label, value):
        lines.append("  %-24s %s" % (label + ":", value))

    line("Container", facts.get("container") or "-")
    line("Exists", facts.get("exists"))
    line("Format", "%s (valid=%s)" % (facts.get("format"), facts.get("format_valid")))
    line("Provisioned (config)", "%s GiB -- configured, not measured"
         % facts.get("provisioned_gb"))
    line("File length", gb_text(facts.get("file_length_gb")))
    line("Allocated on disk", gb_text(facts.get("allocated_gb")))
    line("VIRTUAL CAPACITY", gb_text(facts.get("virtual_capacity_gb")))
    line("Expandable", facts.get("is_expandable"))
    line("Attached / loaded", facts.get("loaded"))
    line("Internal total", gb_text(facts.get("internal_total_gb")))
    line("Internal used", gb_text(facts.get("internal_used_gb")))
    line("Internal free", gb_text(facts.get("internal_free_gb")))
    line("Host volume", facts.get("host_volume") or "UNKNOWN")
    line("Host total", gb_text(facts.get("host_total_gb")))
    line("Host free", gb_text(facts.get("host_free_gb")))
    line("Host used ratio", facts.get("host_used_ratio"))
    line("Measured at", facts.get("measured_at") or "never")
    line("Verdicts", ", ".join(verdicts) or "none")
    line("Resize-VHD", "available" if (toolchain or {}).get("available")
         else "NOT available (%s)" % ((toolchain or {}).get("detail") or "unknown"))

    lines.append("")
    lines.append("PROVENANCE -- where every number came from")
    for key, source in sorted((facts.get("sources") or {}).items()):
        lines.append("  %-24s %s" % (key, source))
    if facts.get("errors"):
        lines.append("")
        lines.append("UNMEASURED / ERRORS")
        for error in facts["errors"]:
            lines.append("  %-24s %s" % (error.get("field"), error.get("message")))
    lines.append("")
    lines.append("File length and virtual capacity are different facts. A dynamic")
    lines.append("VHDX is a few MiB on disk while its virtual size is the whole")
    lines.append("provisioned maximum, so neither number is ever reported as the")
    lines.append("other here.")
    lines.append("")
    lines.append("The internal BitLocker filesystem figures stay UNKNOWN on purpose:")
    lines.append("they can only be read with the volume unlocked, and this window")
    lines.append("never mounts a vault just to print a number.")
    return "\n".join(lines)


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
    requestEnroll = pyqtSignal(str, bool, object)
    requestAck = pyqtSignal(bool)
    requestShutdown = pyqtSignal()
    requestWindowHandle = pyqtSignal(int)
    requestPrivilegedStatus = pyqtSignal()
    requestPrivilegedAction = pyqtSignal(str, int)
    requestImportApp = pyqtSignal(str, str)
    requestRemoveKey = pyqtSignal(str, str)
    requestMigrate = pyqtSignal(str, str)
    requestMigrateRecover = pyqtSignal(str)
    requestChangePortablePassword = pyqtSignal([str, str], [str, str, str])
    requestRemovePortablePassword = pyqtSignal(str)
    requestListProtectors = pyqtSignal(str)
    requestPublishBackup = pyqtSignal(str)
    requestVerifyBackup = pyqtSignal()
    requestCapacity = pyqtSignal(str)
    requestPlanGrow = pyqtSignal(str, int)

    def _wire_signals(self):
        pass  # placeholder if needed

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
        apply_golden_font(QApplication.instance())
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
        self._setup_tab_autoshown = False

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
        self.tabs.addTab(self.tab_setup, "Setup && Security")  # && = literal &, not a mnemonic
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
        self.requestChangePortablePassword[str, str].connect(self.worker.changePortablePassword)
        self.requestChangePortablePassword[str, str, str].connect(self.worker.changePortablePassword)
        self.requestRemovePortablePassword.connect(self.worker.removePortablePassword)
        self.requestListProtectors.connect(self.worker.listProtectors)
        self.requestPublishBackup.connect(self.worker.publishBackup)
        self.requestVerifyBackup.connect(self.worker.verifyBackup)
        self.requestCapacity.connect(self.worker.capacityFacts)
        self.requestPlanGrow.connect(self.worker.planGrow)
        self.worker.capacityReady.connect(self._capacity_ready)
        self.worker.growPlanReady.connect(self._grow_plan_ready)

        self.filter = SessionEventFilter(self.requestSystemEvent.emit)
        QApplication.instance().installNativeEventFilter(self.filter)
        self.filter.register(int(self.winId()))
        self.requestWindowHandle.emit(int(self.winId()))

        #: T-256: a late reply for a profile the operator has already moved
        #: away from is dropped, not rendered against the wrong container.
        self._capacity_last = None
        self._plan_target = None

        self._shutdown_state = self.SHUTDOWN_IDLE
        self._shutdown_report = None
        self._shutdown_watchdog = QTimer(self)
        self._shutdown_watchdog.setSingleShot(True)
        self._shutdown_watchdog.timeout.connect(self._shutdown_timed_out)

        self.thread.start()
        self.requestBootstrap.emit()
        # First capacity read is asynchronous: the panel says "measuring"
        # until the worker answers, and the UI thread never blocks on a disk.
        self.requestCapacity.emit(self._selected)
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
        self.btn_goto_setup = QPushButton("Continue setup")
        self.btn_goto_setup.setObjectName("primary_action")
        self.btn_goto_setup.setMinimumHeight(28)
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

        # Pre-Migration Safe Exit Panel (Phase 2)
        self.safe_exit_panel = QFrame()
        self.safe_exit_panel.setObjectName("panel")
        self.safe_exit_panel.setVisible(False)
        safe_layout = QVBoxLayout(self.safe_exit_panel)
        safe_layout.setContentsMargins(8, 8, 8, 8)
        safe_layout.setSpacing(6)

        self.lbl_safe_exit_headline = QLabel("Setup is incomplete. Your original notes are still available.")
        self.lbl_safe_exit_headline.setStyleSheet("font-weight: bold; color: %s;" % C_BORDER_HL)
        safe_layout.addWidget(self.lbl_safe_exit_headline)

        self.lbl_safe_exit_detail = QLabel()
        self.lbl_safe_exit_detail.setWordWrap(True)
        self.lbl_safe_exit_detail.setObjectName("sub")
        safe_layout.addWidget(self.lbl_safe_exit_detail)

        safe_btn_row = QHBoxLayout()
        safe_btn_row.setSpacing(6)
        self.btn_safe_open_folder = QPushButton("Open original folder")
        self.btn_safe_copy_path = QPushButton("Copy original folder path")
        self.btn_safe_instructions = QPushButton("View emergency instructions")
        self.btn_safe_continue = QPushButton("Continue setup")
        self.btn_safe_continue.setObjectName("primary_action")

        safe_btn_row.addWidget(self.btn_safe_open_folder)
        safe_btn_row.addWidget(self.btn_safe_copy_path)
        safe_btn_row.addWidget(self.btn_safe_instructions)
        safe_btn_row.addWidget(self.btn_safe_continue)
        safe_btn_row.addStretch()
        safe_layout.addLayout(safe_btn_row)

        self.btn_safe_open_folder.clicked.connect(self._on_safe_open_original_folder)
        self.btn_safe_copy_path.clicked.connect(self._on_safe_copy_original_path)
        self.btn_safe_instructions.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
        self.btn_safe_continue.clicked.connect(lambda: self.tabs.setCurrentIndex(1))

        layout.addWidget(self.safe_exit_panel)

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

        # ── Capacity (T-256) ────────────────────────────────────────────
        # Two compact lines instead of a second status grid: the full, sourced
        # breakdown lives in the Details dialog, so the tab keeps its height
        # and nothing is clipped at the supported window sizes.
        cap_panel = QFrame()
        cap_panel.setObjectName("panel")
        cap_layout = QGridLayout(cap_panel)
        cap_layout.setVerticalSpacing(2)
        cap_layout.setHorizontalSpacing(6)
        cap_title = QLabel("CAPACITY:")
        cap_title.setObjectName("sub")
        self.cap_summary = QLabel("measuring...")
        self.cap_summary.setWordWrap(True)
        self.cap_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.btn_cap_details = QPushButton("Details...")
        self.btn_cap_grow = QPushButton("Grow plan...")
        cap_buttons = QHBoxLayout()
        cap_buttons.setSpacing(4)
        cap_buttons.addWidget(self.btn_cap_details)
        cap_buttons.addWidget(self.btn_cap_grow)
        cap_buttons.addStretch()
        cap_layout.addWidget(cap_title, 0, 0)
        cap_layout.addWidget(self.cap_summary, 0, 1)
        cap_layout.addLayout(cap_buttons, 0, 2)
        self.cap_status = QLabel("")
        self.cap_status.setObjectName("sub")
        self.cap_status.setWordWrap(True)
        cap_layout.addWidget(self.cap_status, 1, 0, 1, 3)
        cap_layout.setColumnStretch(1, 1)
        layout.addWidget(cap_panel)

        self.btn_cap_details.clicked.connect(self._show_capacity_details)
        self.btn_cap_grow.clicked.connect(self._request_grow_plan)

    # ══════════════════════════════════════════════════════════ TAB 2: SETUP
    def _build_setup_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        scroll.setWidget(content)
        main_layout = QVBoxLayout(self.tab_setup)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # Next Action Callout Card -- the one control a person needs: where
        # they are, what happens next, and a single Continue button.
        self.action_card = QFrame()
        self.action_card.setObjectName("action_card")
        ac_layout = QVBoxLayout(self.action_card)
        ac_layout.setSpacing(4)
        step_row = QHBoxLayout()
        self.action_card_step = QLabel("")
        self.action_card_step.setObjectName("sub")
        self.setup_progress = QProgressBar()
        self.setup_progress.setRange(0, 8)
        self.setup_progress.setValue(0)
        self.setup_progress.setFormat("")
        step_row.addWidget(self.action_card_step)
        step_row.addWidget(self.setup_progress, stretch=1)
        self.action_card_title = QLabel("NEXT ACTION: EVALUATING...")
        self.action_card_title.setObjectName("title")
        self.action_card_desc = QLabel("Reading system state...")
        self.action_card_desc.setWordWrap(True)
        self.action_card_hint = QLabel("")
        self.action_card_hint.setObjectName("sub")
        self.action_card_hint.setWordWrap(True)
        self.action_card_btn = QPushButton("Execute Next Step")
        self.action_card_btn.setObjectName("primary_action")
        self.action_card_btn.setMinimumHeight(34)
        # Prominent live activity / busy banner
        self.action_busy_frame = QFrame()
        self.action_busy_frame.setObjectName("busy_banner")
        self.action_busy_frame.setStyleSheet(
            "QFrame#busy_banner { background: #2A2410; border: 2px solid #FFC800; border-radius: 4px; padding: 6px; }"
        )
        ab_layout = QVBoxLayout(self.action_busy_frame)
        ab_layout.setContentsMargins(6, 6, 6, 6)
        ab_layout.setSpacing(4)
        self.action_busy_bar = QProgressBar()
        self.action_busy_bar.setRange(0, 0)
        self.action_busy_bar.setTextVisible(False)
        self.action_busy_bar.setFixedHeight(8)
        self.action_busy_text = QLabel("⏳ Выполняется операция... Пожалуйста, подождите")
        self.action_busy_text.setStyleSheet("font-size: 12pt; font-weight: bold; color: #FFE066;")
        self.action_busy_text.setWordWrap(True)
        self.action_busy_detail = QLabel("")
        self.action_busy_detail.setStyleSheet("font-size: 9pt; color: #C5A059;")
        self.action_busy_detail.setWordWrap(True)
        ab_layout.addWidget(self.action_busy_text)
        ab_layout.addWidget(self.action_busy_detail)
        ab_layout.addWidget(self.action_busy_bar)
        self.action_busy_frame.setVisible(False)

        ac_layout.addLayout(step_row)
        ac_layout.addWidget(self.action_card_title)
        ac_layout.addWidget(self.action_card_desc)
        ac_layout.addWidget(self.action_card_hint)
        ac_layout.addWidget(self.action_busy_frame)
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
        self.checklist_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.checklist_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.checklist_table.verticalHeader().setDefaultSectionSize(24)
        self._fit_checklist_height()
        layout.addWidget(self.checklist_table)

        # Step Actions Container -- folded away: Continue above runs these
        # very steps in order, gated by the same state machine. Opened on
        # demand, and automatically while an acceptance run shows its checks.
        self.btn_advanced = QPushButton()
        self.btn_advanced.setObjectName("advanced_toggle")
        self.btn_advanced.setCheckable(True)
        self.btn_advanced.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.btn_advanced)

        panel = QFrame()
        panel.setObjectName("panel")
        p_layout = QVBoxLayout(panel)
        p_layout.setSpacing(6)
        self.setup_advanced_panel = panel
        adv_note = QLabel(
            "You normally don't need these: Continue runs the same steps in "
            "order. Only the step that is due can be clicked here as well.")
        adv_note.setObjectName("sub")
        adv_note.setWordWrap(True)
        p_layout.addWidget(adv_note)
        self.btn_advanced.toggled.connect(self._set_advanced_visible)

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
        self.btn_open_backup.setVisible(True)
        r8.addWidget(self.btn_open_backup)
        r8.addStretch()
        p_layout.addLayout(r8)

        # Step 9 controls. There is deliberately no delete button here: the
        # plaintext is the user's, SAITULS only tells the truth about it and
        # shows where it is.
        r9 = QHBoxLayout()
        r9.addWidget(QLabel("9. Plaintext copy:"))
        self.lbl_plaintext_state = QLabel("")
        self.lbl_plaintext_state.setWordWrap(True)
        r9.addWidget(self.lbl_plaintext_state, 1)
        self.btn_recheck_backup = QPushButton("Re-check")
        r9.addWidget(self.btn_recheck_backup)
        r9.addStretch()
        p_layout.addLayout(r9)

        layout.addWidget(panel)
        layout.addStretch()
        # Long status notes wrap instead of widening the page sideways.
        for note in (self.lbl_uac_note, self.lbl_helper_facts, self.lbl_probe_facts,
                     self.lbl_acc_disclaimer, self.lbl_acc_tokens, self.lbl_import_dest):
            note.setWordWrap(True)
        self._set_advanced_visible(False)

        # Button connects
        self.btn_prep_uac.clicked.connect(self._on_prep_uac)
        self.btn_restart_now.clicked.connect(self._restart_now)
        self.btn_restart_later.clicked.connect(self._restart_later)
        self.btn_install_task.clicked.connect(lambda: self._privileged_action("install"))
        self.btn_test_helper.clicked.connect(self._test_silent_helper)
        self.btn_helper_details.clicked.connect(self._show_helper_details)
        self.btn_repair_task.clicked.connect(lambda: self._privileged_action("repair"))
        self.btn_remove_task.clicked.connect(lambda: self._privileged_action("remove"))
        self.btn_probe_key.clicked.connect(self._probe_key)
        self.btn_run_acceptance.clicked.connect(self._run_acceptance_dialog)
        self.btn_import_obsidian.clicked.connect(self._on_import_obsidian)
        self.btn_enroll_vault.clicked.connect(self._on_enroll_vault)
        self.btn_migrate_vault.clicked.connect(self._on_migrate_vault)
        self.btn_open_backup.clicked.connect(self._open_backup_location)
        self.btn_recheck_backup.clicked.connect(
            lambda: self._invalidate_setup("recheck_backup"))

    def _set_advanced_visible(self, visible):
        self.setup_advanced_panel.setVisible(bool(visible))
        self.btn_advanced.blockSignals(True)
        self.btn_advanced.setChecked(bool(visible))
        self.btn_advanced.blockSignals(False)
        self.btn_advanced.setText(
            ("▾" if visible else "▸")
            + "  Advanced: individual step controls && diagnostics")

    def _fit_checklist_height(self):
        """Show every checklist row at once -- no scrollbar inside the page."""
        table = self.checklist_table
        table.resizeRowsToContents()
        height = table.horizontalHeader().sizeHint().height() + 2 * table.frameWidth()
        for row in range(table.rowCount()):
            height += table.rowHeight(row)
        table.setFixedHeight(height + 2)

    # ══════════════════════════════════════════════════════════ TAB 3: RECOVERY
    def _build_recovery_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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

        self.btn_add_key.clicked.connect(lambda: self.requestEnroll.emit(self._selected, True, None))
        self.btn_remove_key.clicked.connect(self._on_remove_lost_key)

        # Section 1.5: Portable Master Password
        lbl_pmp = QLabel("PORTABLE MASTER PASSWORD")
        lbl_pmp.setObjectName("title")
        layout.addWidget(lbl_pmp)

        panel_pmp = QFrame()
        panel_pmp.setObjectName("panel")
        pp_layout = QVBoxLayout(panel_pmp)
        pmp_desc = QLabel(
            "Native BitLocker password protector on the vault container.\n"
            "Unlocks without SAITULS on any Windows 10/11 Pro machine.\n"
            "Door 2 independent portable access.")
        pmp_desc.setObjectName("sub")
        pmp_desc.setWordWrap(True)
        pp_layout.addWidget(pmp_desc)

        pmp_btn_row = QHBoxLayout()
        self.btn_change_pmp = QPushButton("Change / Set Portable Password")
        self.btn_remove_pmp = QPushButton("Remove Portable Password")
        self.btn_remove_pmp.setObjectName("danger")
        self.btn_list_protectors = QPushButton("List BitLocker Protectors")
        pmp_btn_row.addWidget(self.btn_change_pmp)
        pmp_btn_row.addWidget(self.btn_remove_pmp)
        pmp_btn_row.addWidget(self.btn_list_protectors)
        pmp_btn_row.addStretch()
        pp_layout.addLayout(pmp_btn_row)
        layout.addWidget(panel_pmp)

        self.btn_change_pmp.clicked.connect(self._on_change_portable_password)
        self.btn_remove_pmp.clicked.connect(self._on_remove_portable_password)
        self.btn_list_protectors.clicked.connect(self._on_list_protectors)

        # Section 1.6: Encrypted Backups
        lbl_eb = QLabel("ENCRYPTED BACKUPS")
        lbl_eb.setObjectName("title")
        layout.addWidget(lbl_eb)

        panel_eb = QFrame()
        panel_eb.setObjectName("panel")
        peb_layout = QVBoxLayout(panel_eb)
        eb_desc = QLabel(
            "Atomic ciphertext publication of __STORE.svault with SHA-256 integrity.\n"
            "Backups are stored in __SECURE_BACKUPS for FreeFileSync off-machine sync.\n"
            "Includes standalone emergency recovery documentation and script.")
        eb_desc.setObjectName("sub")
        eb_desc.setWordWrap(True)
        peb_layout.addWidget(eb_desc)

        eb_btn_row = QHBoxLayout()
        self.btn_publish_backup = QPushButton("Publish Backup Now")
        self.btn_publish_backup.setObjectName("primary_action")
        self.btn_verify_backup = QPushButton("Verify Backup Integrity")
        self.btn_open_external = QPushButton("Open Portable Vault...")
        eb_btn_row.addWidget(self.btn_publish_backup)
        eb_btn_row.addWidget(self.btn_verify_backup)
        eb_btn_row.addWidget(self.btn_open_external)
        eb_btn_row.addStretch()
        peb_layout.addLayout(eb_btn_row)
        layout.addWidget(panel_eb)

        self.btn_publish_backup.clicked.connect(self._on_publish_backup)
        self.btn_verify_backup.clicked.connect(self._on_verify_backup)
        self.btn_open_external.clicked.connect(self._on_open_external_vault)

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
        sr_desc.setWordWrap(True)
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
        mr_desc.setWordWrap(True)
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
        self.em_steps = QPlainTextEdit()
        self.em_steps.setReadOnly(True)
        self.em_steps.setObjectName("sub")
        self.em_steps.setFixedHeight(300)
        pem_layout.addWidget(self.em_steps)
        em_btn_row = QHBoxLayout()
        btn_em_copy = QPushButton("Copy steps")
        btn_em_copy.clicked.connect(self._copy_emergency_steps)
        em_btn_row.addWidget(btn_em_copy)
        btn_em_recheck = QPushButton("Re-check")
        btn_em_recheck.clicked.connect(
            lambda: self._render_emergency_steps())
        em_btn_row.addWidget(btn_em_recheck)
        btn_em_run = QPushButton("Run attach + letter step")
        btn_em_run.setObjectName("danger")
        btn_em_run.clicked.connect(self._on_run_emergency_attach)
        em_btn_row.addWidget(btn_em_run)
        btn_em_select = QPushButton("Select image...")
        btn_em_select.clicked.connect(self._on_select_recovery_image)
        em_btn_row.addWidget(btn_em_select)
        em_btn_row.addStretch()
        pem_layout.addLayout(em_btn_row)
        self._render_emergency_steps()
        layout.addWidget(panel_em)

        # Section 5: Mnemonic & Passphrase Recovery Tool
        lbl_mn = QLabel("MNEMONIC & PASSPHRASE RECOVERY TOOL")
        lbl_mn.setObjectName("title")
        layout.addWidget(lbl_mn)

        panel_mn = QFrame()
        panel_mn.setObjectName("panel")
        pmn_layout = QVBoxLayout(panel_mn)
        mn_desc = QLabel(
            "Bi-directional conversion between 12-word BIP-39 mnemonic phrases and 48-digit Microsoft BitLocker recovery keys.")
        mn_desc.setObjectName("sub")
        mn_desc.setWordWrap(True)
        pmn_layout.addWidget(mn_desc)

        pmn_layout.addWidget(QLabel("Input (12 words OR 48-digit numeric key):"))
        self.mnemonic_input = QLineEdit()
        self.mnemonic_input.setPlaceholderText("Enter 12 BIP-39 words OR XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX")
        pmn_layout.addWidget(self.mnemonic_input)

        mn_btn_row = QHBoxLayout()
        btn_w2k = QPushButton("Convert 12 Words -> BitLocker Key")
        btn_k2w = QPushButton("Convert BitLocker Key -> 12 Words")
        btn_w2k.clicked.connect(self._convert_words_to_key)
        btn_k2w.clicked.connect(self._convert_key_to_words)
        mn_btn_row.addWidget(btn_w2k)
        mn_btn_row.addWidget(btn_k2w)
        mn_btn_row.addStretch()
        pmn_layout.addLayout(mn_btn_row)

        pmn_layout.addWidget(QLabel("Converted Result:"))
        self.mnemonic_output = QLineEdit()
        self.mnemonic_output.setReadOnly(True)
        pmn_layout.addWidget(self.mnemonic_output)

        out_btn_row = QHBoxLayout()
        btn_copy_mn_res = QPushButton("Copy converted result")
        btn_copy_mn_res.clicked.connect(lambda: QApplication.clipboard().setText(self.mnemonic_output.text()))
        out_btn_row.addWidget(btn_copy_mn_res)
        self.mnemonic_status = QLabel("")
        self.mnemonic_status.setObjectName("sub")
        out_btn_row.addWidget(self.mnemonic_status)
        out_btn_row.addStretch()
        pmn_layout.addLayout(out_btn_row)

        layout.addWidget(panel_mn)

    def _convert_words_to_key(self):
        text = self.mnemonic_input.text().strip()
        key = sa_mnemonic.mnemonic_to_bitlocker(text)
        if key:
            self.mnemonic_output.setText(key)
            self.mnemonic_status.setText("Valid 12-word mnemonic. Converted to 48-digit BitLocker key.")
        else:
            self.mnemonic_output.clear()
            self.mnemonic_status.setText("Invalid mnemonic phrase. Must be 12 valid BIP-39 words with correct checksum.")

    def _convert_key_to_words(self):
        text = self.mnemonic_input.text().strip()
        words = sa_mnemonic.bitlocker_to_mnemonic(text)
        if words:
            self.mnemonic_output.setText(words)
            self.mnemonic_status.setText("Valid BitLocker key. Converted to 12-word BIP-39 mnemonic phrase.")
        else:
            self.mnemonic_output.clear()
            self.mnemonic_status.setText("Invalid BitLocker key. Must be 8 groups of 6 digits (divisible by 11).")

    def _emergency_container_path(self):
        """The real VHDX path for the selected profile, never a guess."""
        override = getattr(self, "_emergency_image_override", None)
        if override and os.path.isfile(override):
            return override
        try:
            registry = sa_config.load_registry(self.registry_path,
                                               managed_root=self.managed_root)
            prof = registry.get(self._selected)
            if prof and prof.container and os.path.isfile(prof.container):
                return prof.container
            return None
        except Exception:
            return None

    def _on_select_recovery_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select encrypted backup or VHDX image",
            os.path.expanduser("~"),
            "Encrypted Images (*.svault *.vhdx *.vhd);;All Files (*.*)")
        if file_path:
            self._emergency_image_override = file_path
            self._render_emergency_steps()

    def _on_safe_open_original_folder(self):
        safety = getattr(self, "_current_safety_state", None) or {}
        if safety.get("state") in (sa_setup.STATE_CONTAINER_LOCKED, sa_setup.STATE_RECOVERY_READY):
            QMessageBox.warning(self, "Safe Exit",
                                "Encrypted cutover already completed. Plaintext fallback is barred.")
            return
        source = safety.get("source_path")
        if not source or not os.path.isdir(source):
            QMessageBox.warning(self, "Original Folder",
                                "Original notes directory could not be found or is not configured.")
            return
        try:
            os.startfile(source)
        except Exception as exc:
            QMessageBox.warning(self, "Open Folder", "Could not open folder: %s" % exc)

    def _on_safe_copy_original_path(self):
        safety = getattr(self, "_current_safety_state", None) or {}
        source = safety.get("source_path")
        if source:
            QApplication.clipboard().setText(source)
            self._append_log("Copied original notes path to clipboard: %s" % source)

    def _emergency_steps_text(self):
        """Native Windows steps, in an order that actually runs.

        The earlier version handed over a mount command and then a drive
        letter nobody had been told how to find. Windows does not choose one
        for you: the VHDX attaches as a disk with a partition that may have no
        letter at all. Each command below therefore prints what it did, and the
        unlock step uses the letter the previous step produced. No SAITULS
        secret and no YubiKey is involved -- that is the point of this path.
        """
        vhdx = self._emergency_container_path()
        if not vhdx:
            return (
                "CONTAINER PATH: UNKNOWN (Registry or container file unavailable)\n\n"
                "The encrypted container could not be found for profile '%s'.\n"
                "Do NOT guess the container path.\n\n"
                "To recover without the GUI:\n"
                "1. If you have an encrypted backup or image file, click 'Select image...'\n"
                "   below to choose your .svault or .vhdx file.\n"
                "2. Run the standalone PowerShell recovery script:\n"
                "   powershell.exe -File __STORE-RECOVER.ps1 -VaultPath <path-to-image>\n"
                "3. The script attaches in safe READ-ONLY mode by default.\n"
                "4. When prompted, enter your BitLocker recovery password.\n"
                % self._selected
            )
        return (
            "IF ONE KEY IS LOST\n"
            "1. Use another enrolled YubiKey to open the vault normally.\n"
            "2. Back in this Recovery tab, select the lost key and click\n"
            "   'Remove lost key'.\n"
            "\n"
            "IF ALL YUBIKEYS ARE LOST\n"
            "You need the BitLocker recovery password written down during\n"
            "enrollment. Open PowerShell as Administrator and run these in\n"
            "order. Each one prints the result it produced.\n"
            "\n"
            "1. Attach the container read-only (default safe inspection):\n"
            "\n"
            "   $img = \"%s\"\n"
            "   Mount-DiskImage -ImagePath $img -Access ReadOnly\n"
            "   $disk = Get-DiskImage -ImagePath $img | Get-Disk\n"
            "   $part = $disk | Get-Partition | Where-Object DriveLetter\n"
            "   if (-not $part) {\n"
            "       $part = $disk | Get-Partition | New-Partition -AssignDriveLetter\n"
            "   }\n"
            "   $letter = $part.DriveLetter\n"
            "   \"drive letter: $letter`:\"\n"
            "\n"
            "2. Unlock it with the recovery password (the letters go where\n"
            "   $letter is, and the password is the 48-digit key you wrote\n"
            "   down, in its XXXXXX-XXXXXX-... form):\n"
            "\n"
            "   manage-bde -unlock \"$letter`:\" -RecoveryPassword XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX-XXXXXX\n"
            "\n"
            "3. Read the notes as ordinary files at $letter`:\\. This is a\n"
            "   plain Windows folder now, with no SAITULS involved.\n"
            "\n"
            "4. Lock it again when you are done -- do not leave it unlocked:\n"
            "\n"
            "   manage-bde -lock \"$letter`:\"\n"
            "   Dismount-DiskImage -ImagePath $img\n"
            "\n"
            "IF THE PASSWORD IS REFUSED\n"
            "Stop. A refused password means the wrong key or the wrong\n"
            "container -- not that you should try harder. Check that you are\n"
            "on the same machine and the same profile as when you enrolled.\n"
            "\n"
            "IF NO YUBIKEY, NO RECOVERY PASSWORD AND NO BACKUP EXIST\n"
            "The data cannot be recovered. There is no hidden backdoor and no\n"
            "reset path; that is what the encryption is for." % vhdx)

    def _render_emergency_steps(self):
        text = self._emergency_steps_text()
        self.em_steps.setPlainText(text)
        return text

    def _copy_emergency_steps(self):
        # These steps contain no secret -- only a file path and the shape of a
        # password -- so the clipboard is the right tool here.
        QApplication.clipboard().setText(self.em_steps.toPlainText())
        self._append_log("copied emergency recovery steps")

    def _on_run_emergency_attach(self):
        """Run the attach step of the printed procedure, then stop.

        This button deliberately covers only the step that IS automatable
        without any secret: mount + drive letter. The unlock step needs the
        recovery password, and the GUI has no business holding it; running
        the whole sequence here would teach the operator to type their
        recovery password into a dialog, which is exactly the muscle memory
        this product refuses to train. The CLI (`emergency unlock`) asks for
        the password hidden on stdin instead.
        """
        import sa_recovery

        container = self._emergency_container_path()
        if not os.path.isfile(container):
            QMessageBox.warning(
                self, "Emergency recovery",
                "Container not found:\n%s\n\n"
                "Check the selected profile and registry path." % container)
            return
        status, data, detail = sa_recovery.attach_with_letter(container)
        if status == sa_recovery.RETURN_OK:
            letter = (data or {}).get("letter")
            self.em_steps.setPlainText(
                "ATTACHED. Drive letter: %s:\n\n"
                "Next (in an Administrator PowerShell):\n"
                "  manage-bde -unlock %s: -RecoveryPassword <48-digit key>\n\n"
                "or run, from Scripts\\secure_apps:\n"
                "  python sa_cli.py emergency %s unlock\n\n"
                "When finished, lock it back:\n"
                "  python sa_cli.py emergency %s close "
                "(or the printed manage-bde -lock steps).\n\n%s"
                % (letter, letter, self._selected, self._selected,
                   self._render_emergency_steps()))
            self._append_log("emergency attach ok, letter=%s:" % letter)
        else:
            tail = "\n".join(detail or ["(no detail)"])
            self.em_steps.setPlainText(
                "ATTACH %s\n\n%s\n\n--- printed procedure (unchanged) ---\n\n%s"
                % (status.upper(), tail, self._render_emergency_steps()))
            self._append_log("emergency attach refused: %s" % tail)

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
        self.requestCapacity.emit(self._selected)
        self.requestKeys.emit(self._selected)
        self.requestTick.emit()

    # ══════════════════════════════════════════════════ CAPACITY (T-256)
    @pyqtSlot(dict)
    def _capacity_ready(self, payload):
        """Render measured capacity facts. Nothing here mounts or resizes."""
        if payload.get("profile_id") != self._selected:
            return                      # a reply for a profile no longer shown
        if not payload.get("ok"):
            self._capacity_last = None
            self.cap_summary.setText("capacity could not be measured")
            self.cap_status.setText(payload.get("detail") or "unknown error")
            return
        self._capacity_last = payload
        facts = payload["facts"]
        self.cap_summary.setText(capacity_summary_line(facts))
        self.cap_status.setText(capacity_status_line(facts, payload.get("toolchain")))

    def _show_capacity_details(self):
        payload = self._capacity_last
        if not payload or not payload.get("ok"):
            QMessageBox.information(
                self, "Capacity",
                "Capacity is still being measured for the selected profile.")
            return
        facts = payload["facts"]
        dialog = CapacityReportDialog(
            "Secure Apps - Capacity facts",
            capacity_report_text(facts, payload.get("toolchain"),
                                 facts.get("verdicts") or []),
            self)
        dialog.exec()

    def _request_grow_plan(self):
        """Ask for a target, then evaluate a READ-ONLY plan against it."""
        payload = self._capacity_last
        if not payload or not payload.get("ok"):
            QMessageBox.information(
                self, "Grow plan",
                "Capacity must be measured before a grow can be planned.")
            return
        facts = payload["facts"]
        target = GrowTargetDialog(facts.get("virtual_capacity_gb"),
                                  facts.get("provisioned_gb"), self)
        if target.exec() != QDialog.DialogCode.Accepted or target.target_gb is None:
            return
        self._plan_target = target.target_gb
        self._append_log("Evaluating a read-only grow plan for %d GiB (nothing "
                         "is resized)." % target.target_gb)
        self.requestPlanGrow.emit(self._selected, int(target.target_gb))

    @pyqtSlot(dict)
    def _grow_plan_ready(self, payload):
        if payload.get("profile_id") != self._selected:
            return
        if getattr(self, "_plan_target", None) != payload.get("new_gb"):
            return                      # superseded target
        dialog = CapacityReportDialog("Secure Apps - Grow plan (read only)",
                                      grow_plan_text(payload), self)
        dialog.exec()

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
            sa_setup.ACTION_SECURE_PLAINTEXT_BACKUP: ("PLAINTEXT COPY STILL EXISTS", "The encrypted vault is verified, but a readable unencrypted copy of your notes is still on this disk. It can be opened with no YubiKey and no SAITULS.", "Open backup location"),
            sa_setup.ACTION_READY: ("SETUP COMPLETE & READY", "All security gates passed. Obsidian is ready for secure use.", "Open Obsidian"),
        }
        t, d, b = action_titles.get(action, ("UNKNOWN", "State check", "Refresh"))
        hint = SETUP_ACTION_HINTS.get(action, "")
        # "Launch it normally" is wrong advice when Explorer itself is
        # elevated: then every shortcut starts elevated. Measured only here.
        self._shell_elevated = None
        if action == sa_setup.ACTION_RUN_NON_ELEVATED:
            try:
                elevated, kind = sa_win.shell_elevation()
            except Exception:
                elevated, kind = None, None
            if elevated and kind == sa_win.ELEVATION_TYPE_DEFAULT:
                # No split token at all: UAC is not active in this boot.
                self._shell_elevated = "uac_inactive"
                t, d, b, hint = UAC_INACTIVE_CARD
            elif elevated:
                self._shell_elevated = "shell_full"
                t, d, b, hint = SHELL_ELEVATED_CARD
        self.action_card_title.setText("NEXT ACTION: %s" % t)
        self.action_card_desc.setText(d)
        self.action_card_hint.setText(hint)
        self.action_card_hint.setVisible(bool(hint))
        self.action_card_btn.setText(
            b if action in (sa_setup.ACTION_READY, sa_setup.ACTION_RUN_NON_ELEVATED)
            else "Continue: %s" % b)

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
                status_item.setForeground(QColor(C_BORDER_HL))
            elif step["status"] in (sa_setup.STEP_ACTION_REQUIRED, sa_setup.STEP_FAILED):
                status_item.setForeground(QColor(C_DANGER_TEXT))
            elif step["status"] == sa_setup.STEP_REBOOT_REQUIRED:
                status_item.setForeground(QColor(C_BORDER_HL))
            self.checklist_table.setItem(row, 1, status_item)
            self.checklist_table.setItem(row, 2, QTableWidgetItem(step["summary"]))
        self._fit_checklist_height()
        # Row 9's truth is asked here, on every refresh, not remembered from the
        # migration that produced it. A copy deleted an hour ago closes this
        # step; a copy restored from the Recycle Bin reopens it.
        archive = self._plaintext_archive_path()
        self.lbl_plaintext_state.setText(
            "still readable at %s -- vault NOT fully protected" % archive
            if archive else "no readable copy remains")

        # Where the person is: "Step N of 8" and a progress bar of completed steps.
        total = len(checklist)
        done = sum(1 for step in checklist if step["status"] == sa_setup.STEP_COMPLETE)
        current = next((step["index"] for step in checklist
                        if step["status"] != sa_setup.STEP_COMPLETE), None)
        self.setup_progress.setRange(0, max(total, 1))
        self.setup_progress.setValue(done)
        self.setup_progress.setFormat("%d of %d done" % (done, total))
        if action == sa_setup.ACTION_READY or current is None:
            self.action_card_step.setText("SETUP COMPLETE")
        else:
            self.action_card_step.setText("STEP %d OF %d" % (current, total))

        # Enable/Disable Open button in Apps tab
        if action != sa_setup.ACTION_READY:
            self.btn_open.setText("SETUP REQUIRED")
            self.btn_open.setEnabled(False)
            self.btn_open.setVisible(False)
            self.btn_goto_setup.setText(
                "Continue setup (step %d of %d)" % (current, total)
                if current is not None else "Continue setup")
            self.btn_goto_setup.setVisible(True)
        else:
            self.btn_open.setText("Open")
            self.btn_open.setVisible(True)
            # bool(): a broker with no status yet must not hand None to Qt
            self.btn_open.setEnabled(bool(status and status.get("state") not in (
                sa_state.LOCKING, sa_state.AUTHENTICATING, sa_state.UNLOCKING, sa_state.RECOVERY_REQUIRED
            )))
            self.btn_goto_setup.setVisible(False)

        # Update Safe Exit Panel (Phase 2)
        safety = sa_setup.classify_profile_safety_state(
            registry,
            profile_id=self._selected,
            broker_status=status,
            migration_verdict=migration_verdict,
        )
        self._current_safety_state = safety
        if action != sa_setup.ACTION_READY and safety.get("safe_exit_allowed"):
            self.safe_exit_panel.setVisible(True)
            self.lbl_safe_exit_headline.setText(safety.get("headline", ""))
            self.lbl_safe_exit_detail.setText(safety.get("detail", ""))
            self.btn_safe_open_folder.setEnabled(bool(safety.get("source_available")))
            self.btn_safe_copy_path.setEnabled(bool(safety.get("source_path")))
        else:
            self.safe_exit_panel.setVisible(False)

        # First measured verdict: an unfinished setup opens on its own tab so
        # Continue is the first thing seen. Once only -- never yanks the tab
        # away later, and never away from a recovery the Apps tab reports.
        if not self._setup_tab_autoshown:
            self._setup_tab_autoshown = True
            recovering = bool(status and status.get("state") == sa_state.RECOVERY_REQUIRED)
            if (action != sa_setup.ACTION_READY and not recovering
                    and self.tabs.currentIndex() == 0):
                self.tabs.setCurrentIndex(1)

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
        elif (action == sa_setup.ACTION_RUN_NON_ELEVATED
              and getattr(self, "_shell_elevated", None) == "uac_inactive"):
            QMessageBox.information(self, "Restart Windows", UAC_INACTIVE_EXPLANATION)
        elif action == sa_setup.ACTION_RUN_NON_ELEVATED and getattr(self, "_shell_elevated", None):
            QMessageBox.information(self, "Sign Out and Back In", SHELL_ELEVATED_EXPLANATION)
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
            self._probe_key()
        elif action == sa_setup.ACTION_RUN_ACCEPTANCE:
            self._run_acceptance_dialog()
        elif action == sa_setup.ACTION_IMPORT_APPLICATION:
            self._on_import_obsidian()
        elif action == sa_setup.ACTION_ENROLL_PROFILE:
            self._on_enroll_vault()
        elif action == sa_setup.ACTION_MIGRATE_VAULT:
            self._on_migrate_vault()
        elif action == sa_setup.ACTION_SECURE_PLAINTEXT_BACKUP:
            self._on_secure_plaintext_backup()
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
        self._show_busy("Kontrollitakse silent helperit...", "Task Scheduler käivitab taustaprotsessi ilma UAC aknata...")
        self.requestPrivilegedAction.emit("check_start", 0)

    def _probe_key(self):
        if not self._require_setup_action(sa_setup.ACTION_PROBE_FIDO2):
            return
        self._show_busy("Tuvastatakse turvavõtit...", "Päritakse FIDO2 tuge ja hmac-secret laiendust läbi CTAP helperi...")
        self.requestCapabilities.emit(self._selected)

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
        dlg = PortablePasswordEnrollDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        portable_pw = dlg.password
        self._show_busy("Enrolling vault...", "Creating BitLocker VHDX and enrolling key protectors...")
        self.requestEnroll.emit(self._selected, False, portable_pw)

    def _on_change_portable_password(self):
        status = self._profiles_status.get(self._selected, {})
        if status.get("state") == "RUNNING" or status.get("supervisor_running"):
            QMessageBox.warning(
                self, "Application Running",
                "Cannot modify BitLocker protectors while the protected application is running.\n\n"
                "Please close the application first to enter maintenance mode."
            )
            return

        dlg = ChangePortablePasswordDialog(mode="change", parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_pw = dlg.new_password
        old_pw = dlg.old_password
        if not new_pw:
            return
        self._show_busy("Updating portable password...", "Changing native BitLocker password protector...")
        if old_pw:
            self.requestChangePortablePassword[str, str, str].emit(self._selected, new_pw, old_pw)
        else:
            self.requestChangePortablePassword[str, str].emit(self._selected, new_pw)

    def _on_remove_portable_password(self):
        status = self._profiles_status.get(self._selected, {})
        if status.get("state") == "RUNNING" or status.get("supervisor_running"):
            QMessageBox.warning(
                self, "Application Running",
                "Cannot modify BitLocker protectors while the protected application is running.\n\n"
                "Please close the application first to enter maintenance mode."
            )
            return

        ans = QMessageBox.question(
            self, "Remove Portable Password",
            "Are you sure you want to remove the Portable Master Password?\n\n"
            "After removal, you can only unlock the vault using your YubiKey\n"
            "or the 48-digit BitLocker emergency recovery key.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._show_busy("Removing portable password...", "Removing native BitLocker password protector...")
            self.requestRemovePortablePassword.emit(self._selected)

    def _on_list_protectors(self):
        self._show_busy("Listing protectors...", "Querying BitLocker key protectors...")
        self.requestListProtectors.emit(self._selected)

    def _on_publish_backup(self):
        self._show_busy("Publishing encrypted backup...", "Copying detached BitLocker vault and computing SHA-256...")
        self.requestPublishBackup.emit(self._selected)

    def _on_verify_backup(self):
        self._show_busy("Verifying backup...", "Computing SHA-256 and validating manifest...")
        self.requestVerifyBackup.emit()

    def _on_open_external_vault(self):
        dlg = OpenPortableVaultDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data = dlg.result_data
        if not data:
            return
        try:
            pw = data["secret"] if not data["is_recovery"] else None
            rec = data["secret"] if data["is_recovery"] else None
            res = sa_backup.open_portable_vault(
                vault_path=data["vault_path"],
                mount_path=data["mount_path"],
                password=pw,
                recovery_password=rec,
                read_only=data.get("read_only", True),
            )
            if res.get("ok"):
                mode_str = "Read-Only" if data.get("read_only") else "Read-Write"
                QMessageBox.information(
                    self, "Portable Vault Opened",
                    f"Vault successfully mounted at:\n{data['mount_path']}\n\n"
                    f"Mode: {mode_str}\n\n"
                    "Remember to close it when finished using the standalone recovery script or CLI."
                )
            else:
                QMessageBox.warning(
                    self, "Open Failed",
                    f"Failed to open portable vault:\n{res.get('reason')}"
                )
        except Exception as exc:
            QMessageBox.critical(self, "Error Opening Vault", str(exc))

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
            item.setForeground(QColor(C_BORDER_HL))
        elif status == "FAIL":
            item.setForeground(QColor(C_DANGER_TEXT))
        elif status == ACCEPTANCE_RUNNING:
            item.setForeground(QColor(C_TEXT))
        else:
            item.setForeground(QColor(C_MUTED))
        self.table_acceptance.setItem(row, 1, item)

    def _show_busy(self, title, detail=""):
        if hasattr(self, "action_busy_frame"):
            self.action_busy_text.setText(title)
            self.action_busy_detail.setText(detail)
            self.action_busy_frame.setVisible(True)
            self.action_card_btn.setEnabled(False)
            self.action_card_btn.setText("⏳ Пожалуйста, подождите...")
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    def _hide_busy(self):
        if hasattr(self, "action_busy_frame"):
            self.action_busy_frame.setVisible(False)
            self.action_card_btn.setEnabled(True)
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            if hasattr(self, "_current_next_action"):
                self._gate_setup_buttons(self._current_next_action)

    def _on_acceptance_log(self, msg):
        self._append_log(msg)
        s = str(msg).strip()
        if not s or not hasattr(self, "action_busy_frame"):
            return
        if getattr(self, "_pin_dialog_active", False):
            return
        if "ENROLLMENT" in s or "twice" in s.lower() or "2x" in s.lower():
            self.action_busy_frame.setVisible(True)
            self.action_busy_frame.setStyleSheet(
                "QFrame#busy_banner { background: #3B2A0A; border: 3px solid #FFD700; border-radius: 6px; padding: 8px; }"
            )
            self.action_busy_text.setStyleSheet("font-size: 13pt; font-weight: bold; color: #FFF066;")
            self.action_busy_text.setText(tr("banner_touch_twice_title"))
            self.action_busy_detail.setText(tr("banner_touch_twice_detail"))
        elif "pin" in s.lower() and "touch" in s.lower():
            self.action_busy_frame.setVisible(True)
            self.action_busy_frame.setStyleSheet(
                "QFrame#busy_banner { background: #3B2A0A; border: 3px solid #FFD700; border-radius: 6px; padding: 8px; }"
            )
            self.action_busy_text.setStyleSheet("font-size: 13pt; font-weight: bold; color: #FFF066;")
            self.action_busy_text.setText(tr("banner_enter_pin_title"))
            self.action_busy_detail.setText(tr("banner_enter_pin_detail"))
        elif "touch" in s.lower() or "puuduta" in s.lower():
            self.action_busy_frame.setVisible(True)
            self.action_busy_frame.setStyleSheet(
                "QFrame#busy_banner { background: #3B2A0A; border: 3px solid #FFD700; border-radius: 6px; padding: 8px; }"
            )
            self.action_busy_text.setStyleSheet("font-size: 13pt; font-weight: bold; color: #FFF066;")
            self.action_busy_text.setText(tr("banner_touch_now_title"))
            self.action_busy_detail.setText(tr("banner_touch_now_detail"))
        elif "LOCK THE WORKSTATION" in s or "Win+L" in s:
            self.action_busy_frame.setVisible(True)
            self.action_busy_frame.setStyleSheet(
                "QFrame#busy_banner { background: #0F2537; border: 3px solid #00B4D8; border-radius: 6px; padding: 8px; }"
            )
            self.action_busy_text.setStyleSheet("font-size: 13pt; font-weight: bold; color: #90E0EF;")
            self.action_busy_text.setText(tr("banner_lock_title"))
            self.action_busy_detail.setText(tr("banner_lock_detail"))
        elif "BitLocker container" in s or "container" in s.lower():
            self.action_busy_frame.setVisible(True)
            self.action_busy_frame.setStyleSheet(
                "QFrame#busy_banner { background: #2A2410; border: 2px solid #FFC800; border-radius: 4px; padding: 6px; }"
            )
            self.action_busy_text.setStyleSheet("font-size: 11pt; font-weight: bold; color: #FFE066;")
            self.action_busy_text.setText(tr("banner_container_title"))
            self.action_busy_detail.setText(tr("banner_container_detail"))
        elif s.startswith(">>"):
            self.action_busy_frame.setVisible(True)
            self.action_busy_text.setText("⏳ " + s[2:].strip())
            self.action_busy_detail.setText(tr("banner_waiting"))

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
        self._show_busy("Käivitatakse hardware acceptance...", "Alustatakse ajutise BitLocker konteineri testimist...")
        self._acceptance_running = True
        # The live per-check table lives in the Advanced panel: show it.
        self._set_advanced_visible(True)
        self._reset_acceptance_table()
        self._advance_acceptance_cursor()
        self._gate_setup_buttons(self._current_next_action)
        self._acc_worker = AcceptanceWorker(self.registry_path, self.managed_root, self._selected, self)
        self._acc_worker.logMessage.connect(self._on_acceptance_log)
        self._acc_worker.pinRequested.connect(self._prompt_pin)
        self._acc_worker.confirmRequested.connect(self._confirm_request)
        self._acc_worker.pauseRequested.connect(self._pause_request)
        self._acc_worker.checkOutcome.connect(self._acceptance_check_outcome)
        self._acc_worker.acceptanceFinished.connect(self._on_acceptance_worker_finished)
        self._acc_worker.start()

    def _on_acceptance_worker_finished(self, code):
        self._acceptance_running = False
        self._hide_busy()
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

    def _on_secure_plaintext_backup(self):
        """Show where the readable copy is and what it costs. Never delete it.

        Deleting the user's notes is their decision, and it is one this program
        is not going to make for them by pressing a button on a setup page.
        What SAITULS owes them is the truth: this copy is readable right now,
        without YubiKey, and the vault is not protected until it is gone.
        """
        archive = self._plaintext_archive_path()
        if not archive:
            self._invalidate_setup("plaintext_backup_gone")
            return
        ans = QMessageBox.question(
            self, "Plaintext copy still exists",
            "This folder still holds a readable, unencrypted copy of your "
            "notes:\n\n%s\n\n"
            "Anyone signed in to this Windows account can open it now, with no "
            "YubiKey and no SAITULS involved. Your encrypted vault is verified, "
            "but it is NOT fully protected while this copy exists.\n\n"
            "SAITULS will not delete your notes for you. Open the folder, check "
            "it is what you expect, then delete it yourself (or move it to "
            "encrypted offline storage). Press RE-CHECK afterwards and this "
            "step closes.\n\n"
            "Note: deleting a file on an SSD does not securely erase it. "
            "Physical destruction or full-disk encryption is what removes that "
            "residual risk." % archive,
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Open)
        if ans == QMessageBox.StandardButton.Open:
            self._open_backup_location()

    def _plaintext_archive_path(self):
        """The archive path if a readable copy still exists, else None."""
        state = self._live_migration_state(self._selected)
        archive = state.get("plaintext_archive") or state.get("source")
        if archive and sa_migrate.plaintext_copy_remains(archive):
            return archive
        return None

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
            # Capacity is measured asynchronously; the panel keeps showing the
            # previous profile's numbers until this profile's answer arrives.
            self.cap_summary.setText("measuring...")
            self.requestCapacity.emit(self._selected)

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
        self._hide_busy()

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
        self._hide_busy()
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

    @pyqtSlot(object, str)
    @pyqtSlot(object)
    def _prompt_pin(self, response_queue, prompt_text="Enter FIDO2 Security Key PIN:"):
        self._pin_dialog_active = True
        self._show_busy(
            tr("banner_enter_pin_title"),
            tr("banner_enter_pin_detail")
        )
        dialog = PinDialog(self, prompt=prompt_text)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._pin_dialog_active = False
                pin_val = dialog.get_pin()
                response_queue.put(pin_val)
                self._show_busy(
                    tr("banner_touch_now_title"),
                    tr("banner_touch_now_detail")
                )
            else:
                self._pin_dialog_active = False
                dialog.entry.clear()
                response_queue.put(None)
                self._hide_busy()
        except Exception:
            self._pin_dialog_active = False
            response_queue.put(None)
            self._hide_busy()

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

    def _live_migration_state(self, profile_id):
        """Re-ask the journal what the protection state actually is right now.

        A migration report is a statement about one moment. The plaintext
        archive can be deleted a second later, and the UI has to stop claiming
        full protection the moment it is -- not when the dialog was composed.
        """
        try:
            registry = sa_config.load_registry(self.registry_path,
                                               managed_root=self.managed_root)
            journal_path = ntpath.join(registry.state_dir,
                                        "migration-%s.json" % profile_id)
            return sa_migrate.inspect(journal_path)
        except Exception:
            return {}

    def _migration_verdict_text(self, payload):
        """Two states, never conflated into one reassuring word.

        MIGRATION_VERIFIED says the encrypted vault's bytes were compared
        against the plaintext and matched. FULL_PROTECTION_COMPLETE says there
        is no readable copy left anywhere. The first does not imply the second,
        and the UI must never print the second while the first is all it knows.
        """
        result = payload.get("result") or {}
        live = self._live_migration_state(payload.get("profile_id") or self._selected)
        archive = (live.get("plaintext_archive") or result.get("plaintext_archive"))
        remains = live.get("plaintext_copy_remains")
        if remains is None:
            # inspect() could not answer (no journal yet, unreadable state):
            # fall back to asking the filesystem directly, and if even that
            # fails, assume the copy is still there.
            remains = sa_migrate.plaintext_copy_remains(archive)
        verified = ("Encrypted vault: VERIFIED (%d files, every file hashed)"
                    % (result.get("file_count") or 0))
        if remains:
            return (
                "%s\n"
                "Full protection: NO -- a readable plaintext copy still exists\n\n"
                "The encrypted vault is verified byte for byte and is now the "
                "one you use. This copy:\n%s\n"
                "can still be read by anyone with this Windows account, with no "
                "YubiKey and no SAITULS involvement. Until it is gone, this "
                "vault is encrypted but NOT protected.\n\n"
                "Delete it yourself, or move it to encrypted offline storage, "
                "then press RE-CHECK. SAITULS will not delete your notes for "
                "you." % (verified, archive))
        return ("%s\n"
                "Full protection: YES\n\n"
                "No readable plaintext copy remains. This vault is now fully "
                "protected." % verified)

    @pyqtSlot(dict)
    def _apply_operation(self, payload):
        was_long = payload.get("action") in (
            "migrate", "import_app", "enroll", "remove_key") or payload.get("ok") is not None
        ok = payload.get("ok")
        action = payload.get("action", "")
        reason = payload.get("reason", "")
        self._append_log("%s %s: %s" % ("OK  " if ok else "FAIL", action or payload.get("profile_id"), reason))
        if action == "migrate" and ok:
            QMessageBox.information(
                self, "Migration Complete",
                self._migration_verdict_text(payload))
        elif action == "change_portable_password" and ok:
            QMessageBox.information(self, "Portable Master Password", reason or "Portable master password updated.")
        elif action == "remove_portable_password" and ok:
            QMessageBox.information(self, "Portable Master Password", reason or "Portable master password removed.")
        elif action == "publish_backup" and ok:
            QMessageBox.information(self, "Encrypted Backup", reason or "Encrypted vault backup published successfully.")
        elif action == "verify_backup" and ok:
            QMessageBox.information(self, "Backup Verification", reason or "Backup integrity verified successfully.")
        elif action == "list_protectors" and ok:
            protectors = payload.get("protectors", [])
            lines = ["BitLocker Key Protectors on Vault:"]
            if not protectors:
                lines.append("  (No protectors found or volume detached)")
            for p in protectors:
                lines.append("  * %s (ID: %s)" % (p.get("ProtectorType", "Unknown"), p.get("KeyProtectorId", "?")))
            QMessageBox.information(self, "BitLocker Protectors", "\n".join(lines))
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
    # UI.md: scale by whole factors only -- a fractional (125%/150%) factor
    # re-blurs bevels and pixmaps even with aliased text. Must precede the
    # QApplication.
    if QApplication.instance() is None:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.Round)
    guard = sa_broker.SingleInstanceGuard()
    if not guard.acquire():
        app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
        apply_golden_font(app)
        app.setStyleSheet(QSS)
        QMessageBox.information(
            None, "Secure Apps Already Running",
            "Another instance of Secure Apps or its broker is already active.\n"
            "Please switch to the existing window.")
        return 0

    app = QApplication.instance() or QApplication(list(argv) if argv is not None else sys.argv)
    apply_golden_font(app)
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
