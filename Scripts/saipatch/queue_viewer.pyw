"""
queue_viewer.pyw - Queue Viewer Cockpit V1 for OpenCode (SAIPATCH).
Read-only SQLite observer + native V2 prompt admission.
Zero database writes. Zero blocking I/O on Qt GUI thread.
Strictly follows UI.md Golden Default (Win95 dark golden, Verdana NoAntialias, 2px bevels, zero white).
Built with PyQt6.

Threading contract (V1):
- ALL worker control flows through queued Qt signals connected AFTER
  moveToThread. The GUI never calls worker methods directly.
- Zero DB / HTTP / process / file I/O runs synchronously on the GUI thread.
- Shutdown: disable controls -> emit queued stop commands -> each worker
  stops its own timers/resources in its own thread -> stopped signal ->
  thread.quit() -> bounded wait -> close.
"""
import os
import sys
import time
import json
from datetime import datetime
from typing import Optional, List

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSlot, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QLabel, QLineEdit, QTextEdit,
    QFrame, QAbstractItemView, QCheckBox, QFileDialog
)

# Import sibling modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import viewer_observer as obs
import viewer_endpoint as ep
import viewer_sender as sender_mod
import viewer_presets as presets
import viewer_live_stream as lstream
import viewer_presentation as present

# ── Golden Default Palette ──────────────────────────────────────────
C_BG = "#1A1810"
C_BG_SOFT = "#232018"
C_SURFACE = "#332E22"
C_SURFACE_RAISED = "#3D372A"
C_SURFACE_ALT = "#453D30"
C_BORDER_DARK = "#100E08"
C_BORDER_HL = "#F0D060"
C_BEVEL_LIGHT = "#75663D"
C_BORDER_MUTED = "#5A5040"
C_TEXT_PRIMARY = "#D4C89A"
C_TEXT_SECONDARY = "#9C9371"
C_TEXT_MUTED = "#6E674E"
C_ACCENT_TEAL = "#008080"
C_DANGER = "#7A2020"
C_DANGER_TEXT = "#D66464"
C_SUCCESS = "#4A7A20"
C_SELECTION = "#3D372A"

GOLDEN_DEFAULT_QSS = f"""
* {{
    font-family: 'Verdana', sans-serif;
    font-size: 11px;
    border-radius: 0px;
}}
QWidget {{
    background-color: {C_BG};
    color: {C_TEXT_PRIMARY};
    selection-background-color: {C_SELECTION};
    selection-color: {C_BORDER_HL};
}}
QMainWindow, QDialog {{
    background-color: {C_BG};
}}
QFrame#sidebarFrame {{
    background-color: {C_BG_SOFT};
    border: 2px solid {C_BORDER_DARK};
    border-top-color: {C_BEVEL_LIGHT};
    border-left-color: {C_BEVEL_LIGHT};
    border-bottom-color: {C_BORDER_DARK};
    border-right-color: {C_BORDER_DARK};
}}
QFrame#statusCard {{
    background-color: {C_BG_SOFT};
    border: 2px solid {C_BORDER_DARK};
    border-top-color: {C_BORDER_DARK};
    border-left-color: {C_BORDER_DARK};
    border-bottom-color: {C_BEVEL_LIGHT};
    border-right-color: {C_BEVEL_LIGHT};
    padding: 6px;
}}
QFrame#toolbarFrame {{
    background-color: {C_SURFACE};
    border: 2px solid {C_BORDER_DARK};
    border-top-color: {C_BEVEL_LIGHT};
    border-left-color: {C_BEVEL_LIGHT};
    border-bottom-color: {C_BORDER_DARK};
    border-right-color: {C_BORDER_DARK};
}}
QFrame#liveOutputFrame {{
    background-color: #14120C;
    border: 2px solid {C_BORDER_DARK};
    border-top-color: {C_BORDER_DARK};
    border-left-color: {C_BORDER_DARK};
    border-bottom-color: {C_BEVEL_LIGHT};
    border-right-color: {C_BEVEL_LIGHT};
}}
QListWidget {{
    background-color: {C_BG_SOFT};
    color: {C_TEXT_PRIMARY};
    border: 1px solid {C_BORDER_DARK};
    outline: none;
}}
QListWidget::item {{
    padding: 6px 8px;
    border-bottom: 1px solid {C_SURFACE};
}}
QListWidget::item:selected {{
    background-color: {C_SELECTION};
    color: {C_BORDER_HL};
    border-left: 3px solid {C_BORDER_HL};
}}
QListWidget::item:hover:!selected {{
    background-color: #2A251C;
}}
QTableWidget {{
    background-color: {C_BG_SOFT};
    color: {C_TEXT_PRIMARY};
    border: 2px solid {C_BORDER_DARK};
    border-top-color: {C_BORDER_DARK};
    border-left-color: {C_BORDER_DARK};
    border-bottom-color: {C_BEVEL_LIGHT};
    border-right-color: {C_BEVEL_LIGHT};
    gridline-color: {C_SURFACE};
    outline: none;
}}
QTableWidget::item {{
    padding: 4px 6px;
    border-bottom: 1px solid #2B251B;
}}
QTableWidget::item:selected {{
    background-color: {C_SELECTION};
    color: {C_BORDER_HL};
}}
QHeaderView::section {{
    background-color: {C_SURFACE};
    color: {C_BORDER_HL};
    border: 1px solid {C_BORDER_DARK};
    padding: 4px 6px;
    font-weight: bold;
    font-size: 11px;
}}
QPushButton {{
    background-color: {C_SURFACE_RAISED};
    color: {C_TEXT_PRIMARY};
    border-top: 2px solid {C_BEVEL_LIGHT};
    border-left: 2px solid {C_BEVEL_LIGHT};
    border-bottom: 2px solid {C_BORDER_DARK};
    border-right: 2px solid {C_BORDER_DARK};
    padding: 4px 10px;
    font-weight: bold;
    font-size: 11px;
}}
QPushButton:hover {{
    background-color: {C_SURFACE_ALT};
    color: {C_BORDER_HL};
}}
QPushButton:pressed {{
    border-top: 2px solid {C_BORDER_DARK};
    border-left: 2px solid {C_BORDER_DARK};
    border-bottom: 2px solid {C_BEVEL_LIGHT};
    border-right: 2px solid {C_BEVEL_LIGHT};
    padding: 5px 9px 3px 11px;
}}
QPushButton:disabled {{
    background-color: #242017;
    color: {C_BORDER_MUTED};
    border-color: {C_SURFACE};
}}
QPushButton#btnPrimary {{
    color: {C_BORDER_HL};
    border-top-color: #9C8542;
    border-left-color: #9C8542;
}}
QPushButton#btnAudit {{
    background-color: {C_SURFACE};
    color: {C_ACCENT_TEAL};
    font-size: 10px;
    padding: 3px 6px;
}}
QPushButton#btnAudit:hover {{
    color: {C_BORDER_HL};
}}
QPushButton#btnCc {{
    background-color: {C_SURFACE};
    color: {C_BORDER_HL};
    font-weight: bold;
    padding: 3px 8px;
    min-width: 24px;
}}
QLineEdit, QTextEdit {{
    background-color: #14120C;
    color: {C_TEXT_PRIMARY};
    border-top: 2px solid {C_BORDER_DARK};
    border-left: 2px solid {C_BORDER_DARK};
    border-bottom: 2px solid {C_BEVEL_LIGHT};
    border-right: 2px solid {C_BEVEL_LIGHT};
    padding: 4px 6px;
    selection-background-color: {C_SELECTION};
    selection-color: {C_BORDER_HL};
}}
QLineEdit:focus, QTextEdit:focus {{
    border-bottom-color: {C_BORDER_HL};
    border-right-color: {C_BORDER_HL};
}}
QCheckBox {{
    color: {C_TEXT_SECONDARY};
    spacing: 6px;
}}
QCheckBox::indicator {{
    width: 13px;
    height: 13px;
    background-color: #14120C;
    border-top: 1px solid {C_BORDER_DARK};
    border-left: 1px solid {C_BORDER_DARK};
    border-bottom: 1px solid {C_BEVEL_LIGHT};
    border-right: 1px solid {C_BEVEL_LIGHT};
}}
QCheckBox::indicator:checked {{
    background-color: {C_BORDER_HL};
    border: 1px solid {C_BORDER_DARK};
}}
QSplitter::handle {{
    background-color: {C_BORDER_DARK};
    width: 4px;
}}
QScrollBar:vertical {{
    background: {C_BG};
    width: 14px;
    margin: 14px 0 14px 0;
    border: 1px solid {C_BORDER_DARK};
}}
QScrollBar::handle:vertical {{
    background: {C_SURFACE_RAISED};
    border-top: 1px solid {C_BEVEL_LIGHT};
    border-left: 1px solid {C_BEVEL_LIGHT};
    border-bottom: 1px solid {C_BORDER_DARK};
    border-right: 1px solid {C_BORDER_DARK};
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    background: {C_SURFACE};
    border: 1px solid {C_BORDER_DARK};
    height: 14px;
    subcontrol-origin: margin;
}}
QScrollBar::sub-line:vertical {{ subcontrol-position: top; }}
QScrollBar::add-line:vertical {{ subcontrol-position: bottom; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: {C_BG_SOFT};
}}
QScrollBar:horizontal {{
    background: {C_BG};
    height: 14px;
    margin: 0 14px 0 14px;
    border: 1px solid {C_BORDER_DARK};
}}
QScrollBar::handle:horizontal {{
    background: {C_SURFACE_RAISED};
    border: 1px solid {C_BEVEL_LIGHT};
    min-width: 20px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    background: {C_SURFACE};
    border: 1px solid {C_BORDER_DARK};
    width: 14px;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: {C_BG_SOFT};
}}
"""

# ── Sort priority for sessions ──────────────────────────────────────
_STATE_SORT_ORDER = {
    "NEEDS_HUMAN": 0,
    "FAILED": 1,
    "RUNNING": 2,
    "IDLE": 3,
}


# ── Immutable request envelope (dataclasses available) ─────────────
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class AuditRequest:
    """Immutable target envelope captured at CLICK time.

    The target captured here is the only target for the operation: a later
    sidebar selection can NEVER retarget it. The sender worker revalidates
    the captured authority (pid/start-time alive, exact session still on
    the captured endpoint) before POSTing and fails closed on any change.
    """
    request_id: str
    kind: str                       # 'core' | 'wave2' | 'performance' | 'audit-file'
    session_id: str
    project_name: str
    resolver_generation: int
    endpoint_pid: Optional[int]
    endpoint_start_time: Optional[float]
    endpoint_url: Optional[str]
    slot: str = ""
    path: str = ""


class PresetWorker(QObject):
    """Off-GUI-thread preset/file I/O worker.

    All filesystem validation/read/write for presets and audit files runs
    here (paths may be slow/offline/removable). Retains the 1 MiB limit,
    UTF-8 requirement and binary/NUL rejection. Never logs file contents.
    """
    presets_loaded = pyqtSignal(object)          # Dict[slot, path]
    audit_file_loaded = pyqtSignal(object, str)  # (AuditRequest envelope, text)
    file_error = pyqtSignal(str, object)         # (message, request envelope or None)
    preset_saved = pyqtSignal(str, str)          # (slot, path)
    stopped = pyqtSignal()
    # Queued command signals (GUI -> worker). Connected AFTER moveToThread.
    refresh_requested = pyqtSignal()
    path_requested = pyqtSignal(str)
    save_requested = pyqtSignal(str, str)
    read_requested = pyqtSignal(object)          # AuditRequest envelope
    stop_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._cache: dict = {}

    @pyqtSlot()
    def start(self):
        self.refresh_presets()

    @pyqtSlot()
    def refresh_presets(self):
        """Load configured preset paths (worker thread I/O)."""
        try:
            self._cache = presets.load_presets()
        except Exception as e:
            self._cache = {}
            self.file_error.emit(f"Preset load failed: {type(e).__name__}")
        self.presets_loaded.emit(dict(self._cache))

    @pyqtSlot(str)
    def preset_path(self, slot: str):
        """Reply with cached path for a slot (no I/O, worker thread)."""
        self.presets_loaded.emit(dict(self._cache))

    @pyqtSlot(str, str)
    def save_preset(self, slot: str, path: str):
        """Persist a preset mapping (worker thread I/O)."""
        try:
            presets.save_preset(slot, path)
            self._cache[slot] = path
            self.preset_saved.emit(slot, path)
            self.presets_loaded.emit(dict(self._cache))
        except Exception as e:
            self.file_error.emit(f"Preset save failed: {type(e).__name__}")

    @pyqtSlot(object)
    def read_audit_file(self, request):
        """Validate + read the audit file for an IMMUTABLE request envelope
        (worker thread I/O). The captured target travels with the request;
        the reply echoes the SAME envelope so the GUI can never retarget.

        slot may be "" for a plain audit-file send. Enforces:
        1 MiB limit, UTF-8 requirement, binary/NUL rejection. Never logs
        or emits file contents in errors.
        """
        try:
            path = getattr(request, "path", "")
            slot = getattr(request, "slot", "")
            size = os.path.getsize(path)
            if size > presets.MAX_FILE_SIZE:
                self.file_error.emit(f"File too large: {size} bytes", request)
                return
            with open(path, "rb") as f:
                raw = f.read(presets.MAX_FILE_SIZE + 1)
            if b"\x00" in raw:
                self.file_error.emit("Binary file detected", request)
                return
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                self.file_error.emit("File is not valid UTF-8 text", request)
                return
            self.audit_file_loaded.emit(request, text)
        except OSError as e:
            self.file_error.emit(f"Read error: {type(e).__name__}", request)
        except Exception as e:
            self.file_error.emit(f"Read error: {type(e).__name__}", request)

    @pyqtSlot()
    def _on_stop(self):
        self.stopped.emit()

    def stop(self):
        self._on_stop()


class QueueViewerWindow(QMainWindow):
    """Queue Viewer Cockpit V1 — read-only observer + native control client."""

    def __init__(self, db_path: str, initial_session_id: Optional[str] = None):
        super().__init__()
        self.db_path = db_path
        self.current_session_id: Optional[str] = initial_session_id
        self.sessions: list = []
        self.current_queue: list = []
        self._pinned_session_row: int = -1
        self._last_event_time: float = 0.0
        self._session_unavailable: bool = False
        self._closing: bool = False
        self._preset_paths: dict = {}
        self._pending_audit_slot: Optional[str] = None

        # Resolver request identity: EVERY session selection increments the
        # generation; a resolver result is accepted only when its generation
        # AND session match the current selection (stale results discarded).
        self._resolution_generation: int = 0

        # Composer identity: bumped on every USER edit (textEdited). A send
        # captures the revision at submission; the completion handler may
        # clear the composer only when the revision is unchanged, so newer
        # user text is never destroyed by an older completion.
        self._composer_rev: int = 0
        self._composer_submit: Optional[dict] = None
        self._inflight_sends: dict = {}

        # GUI-side ADMISSION OWNERSHIP (single-flight, minted BEFORE any
        # worker dispatch). One owner per admission path; the reservation
        # begins at USER ACTIVATION and is released only by the matching
        # request_id/batch_id on any terminal path. This prevents two rapid
        # activations from both entering the worker queue.
        self._admission_owner: Optional[dict] = None

        # PRESENTATION AUTHORITY (P0): ONE reconciliation owner for the
        # LIVE OUTPUT. Observer durable text and SSE live events BOTH enter
        # this presenter; nothing else writes txt_live_output.
        self._presenter = present.LiveOutputPresenter()
        self._presenter.signals.text_updated.connect(self._on_presented_text)
        self._presenter.signals.activity_updated.connect(self._on_presented_activity)
        # Target B: presenter lifecycle state is consumed by PRODUCTION —
        # the selected-session header/status reacts to live lifecycle
        # events (RUNNING/IDLE/FAILED/NEEDS_HUMAN) before SQLite catches up.
        self._presenter.signals.state_updated.connect(self._on_presented_state)
        self._presenter.set_session(self.current_session_id)

        # Transport state
        self._endpoint_status: ep.EndpointStatus = ep.EndpointStatus("UNAVAILABLE", None, "Starting...")
        self._observer_status: str = "STARTING"
        self._observer_age: float = 0.0
        self._last_activity_line: str = ""
        self._stream_status: str = "OFFLINE"
        self._stream_endpoint: Optional[str] = None

        self.setWindowTitle("SAIPATCH Queue Viewer Cockpit")
        self.resize(1000, 700)
        self.setMinimumSize(720, 500)

        self._build_ui()
        self._start_workers()
        self._update_send_controls()

    # ── UI Construction ─────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter, 1)

        # ── Left: Sessions Sidebar ──
        sidebar_frame = QFrame()
        sidebar_frame.setObjectName("sidebarFrame")
        sidebar_frame.setMinimumWidth(200)
        sidebar_frame.setMaximumWidth(300)
        sidebar_layout = QVBoxLayout(sidebar_frame)
        sidebar_layout.setContentsMargins(6, 6, 6, 6)
        sidebar_layout.setSpacing(4)

        lbl_sidebar = QLabel("SESSIONS")
        lbl_sidebar.setStyleSheet(f"color: {C_BORDER_HL}; font-weight: bold;")
        sidebar_layout.addWidget(lbl_sidebar)

        self.session_list = QListWidget()
        self.session_list.currentRowChanged.connect(self._on_session_selected)
        sidebar_layout.addWidget(self.session_list)

        # Pin checkbox
        self.chk_pin = QCheckBox("Pin (Always on Top)")
        self.chk_pin.toggled.connect(self._toggle_pin)
        sidebar_layout.addWidget(self.chk_pin)

        splitter.addWidget(sidebar_frame)

        # ── Right: Main Panel ──
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        # ── Header bar ──
        header = QFrame()
        header.setObjectName("statusCard")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(8, 6, 8, 6)
        header_layout.setSpacing(2)

        row1 = QHBoxLayout()
        self.lbl_session_name = QLabel("No session selected")
        self.lbl_session_name.setStyleSheet(f"color: {C_BORDER_HL}; font-weight: bold; font-size: 12px;")
        row1.addWidget(self.lbl_session_name)
        row1.addStretch()
        self.lbl_queue_count = QLabel("Q: 0")
        self.lbl_queue_count.setStyleSheet(f"color: {C_ACCENT_TEAL}; font-weight: bold;")
        row1.addWidget(self.lbl_queue_count)
        header_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.lbl_session_dir = QLabel("")
        self.lbl_session_dir.setStyleSheet(f"color: {C_TEXT_MUTED};")
        row2.addWidget(self.lbl_session_dir)
        row2.addStretch()
        self.lbl_native_status = QLabel("[STARTING]")
        self.lbl_native_status.setStyleSheet(f"color: {C_TEXT_SECONDARY}; font-weight: bold;")
        row2.addWidget(self.lbl_native_status)
        header_layout.addLayout(row2)

        right_layout.addWidget(header)

        # ── LIVE OUTPUT ──
        live_label = QLabel("LIVE OUTPUT")
        live_label.setStyleSheet(f"color: {C_TEXT_MUTED}; font-weight: bold; font-size: 10px;")
        right_layout.addWidget(live_label)

        self.txt_live_output = QTextEdit()
        self.txt_live_output.setReadOnly(True)
        self.txt_live_output.setObjectName("liveOutputFrame")
        self.txt_live_output.setStyleSheet(
            f"QTextEdit {{ font-family: 'Consolas', 'Courier New', monospace; font-size: 11px; "
            f"background-color: #14120C; color: {C_TEXT_PRIMARY}; }}"
        )
        self.txt_live_output.setMinimumHeight(120)
        right_layout.addWidget(self.txt_live_output, 3)

        # ── Activity line ──
        self.lbl_activity = QLabel("")
        self.lbl_activity.setStyleSheet(
            f"color: {C_TEXT_SECONDARY}; font-size: 10px; padding: 2px 4px; "
            f"background-color: {C_SURFACE};"
        )
        right_layout.addWidget(self.lbl_activity)

        # ── NEXT QUEUE ──
        queue_label = QLabel("NEXT QUEUE")
        queue_label.setStyleSheet(f"color: {C_TEXT_MUTED}; font-weight: bold; font-size: 10px;")
        right_layout.addWidget(queue_label)

        self.table_queue = QTableWidget()
        self.table_queue.setColumnCount(3)
        self.table_queue.setHorizontalHeaderLabels(["#", "Prompt", "Age"])
        self.table_queue.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_queue.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_queue.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_queue.verticalHeader().setVisible(False)
        self.table_queue.horizontalHeader().setStretchLastSection(False)
        hdr = self.table_queue.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table_queue.setColumnWidth(0, 32)
        self.table_queue.setColumnWidth(2, 60)
        self.table_queue.setMinimumHeight(80)
        self.table_queue.setMaximumHeight(200)
        right_layout.addWidget(self.table_queue, 1)

        # ── Composer bar ──
        composer_frame = QFrame()
        composer_frame.setObjectName("toolbarFrame")
        composer_layout = QVBoxLayout(composer_frame)
        composer_layout.setContentsMargins(6, 6, 6, 6)
        composer_layout.setSpacing(4)

        # Input row
        input_row = QHBoxLayout()
        self.input_prompt = QLineEdit()
        self.input_prompt.setPlaceholderText("Type prompt and press Enter or Send...")
        self.input_prompt.returnPressed.connect(self._on_send)
        input_row.addWidget(self.input_prompt)

        self.btn_send = QPushButton("Send")
        self.btn_send.setObjectName("btnPrimary")
        self.btn_send.clicked.connect(self._on_send)
        input_row.addWidget(self.btn_send)
        composer_layout.addLayout(input_row)

        # Audit buttons row
        audit_row = QHBoxLayout()

        self.btn_cc = QPushButton("cc")
        self.btn_cc.setObjectName("btnCc")
        self.btn_cc.setToolTip("Send literal 'cc' to continue")
        self.btn_cc.clicked.connect(self._on_cc)
        audit_row.addWidget(self.btn_cc)

        audit_row.addSpacing(8)

        self.btn_core = QPushButton("CORE")
        self.btn_core.setObjectName("btnAudit")
        self.btn_core.clicked.connect(self._on_core)
        audit_row.addWidget(self.btn_core)

        self.btn_w2 = QPushButton("W2")
        self.btn_w2.setObjectName("btnAudit")
        self.btn_w2.clicked.connect(self._on_w2)
        audit_row.addWidget(self.btn_w2)

        self.btn_perf = QPushButton("PERF")
        self.btn_perf.setObjectName("btnAudit")
        self.btn_perf.clicked.connect(self._on_perf)
        audit_row.addWidget(self.btn_perf)

        self.btn_3waves = QPushButton("3 WAVES")
        self.btn_3waves.setObjectName("btnAudit")
        self.btn_3waves.clicked.connect(self._on_3waves)
        audit_row.addWidget(self.btn_3waves)

        self.btn_audit_file = QPushButton("Audit File...")
        self.btn_audit_file.setObjectName("btnAudit")
        self.btn_audit_file.clicked.connect(self._on_audit_file)
        audit_row.addWidget(self.btn_audit_file)

        audit_row.addStretch()

        # Preset config context menu on right-click
        for btn, slot in [(self.btn_core, "core"), (self.btn_w2, "wave2"), (self.btn_perf, "performance")]:
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _, s=slot: self._on_change_preset(s)
            )
            btn.setToolTip("Not configured. Click to select file.\nRight-click to change")

        composer_layout.addLayout(audit_row)

        # Feedback label
        self.lbl_feedback = QLabel("")
        self.lbl_feedback.setStyleSheet(f"color: {C_TEXT_SECONDARY}; font-size: 10px;")
        composer_layout.addWidget(self.lbl_feedback)

        right_layout.addWidget(composer_frame)

        splitter.addWidget(right_panel)
        splitter.setSizes([220, 780])

        # ── Bottom status bar ──
        status_bar = QHBoxLayout()
        status_bar.setContentsMargins(4, 2, 4, 2)

        self.lbl_db_health = QLabel("DB: ...")
        self.lbl_db_health.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px;")
        status_bar.addWidget(self.lbl_db_health)

        status_bar.addStretch()

        self.lbl_oc_health = QLabel("OC: ...")
        self.lbl_oc_health.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px;")
        status_bar.addWidget(self.lbl_oc_health)

        status_bar.addStretch()

        # Transport truth for realtime token projection, semantically
        # separate from DB/OC health and execution state:
        #   LIVE          SSE /api/event connected and draining
        #   RECONNECTING  SSE down, backoff reconnects in progress
        #   DB-ONLY       endpoint resolved but SSE not (yet) carrying data
        #   OFFLINE       no resolved endpoint / no session
        self.lbl_stream_status = QLabel("STREAM: OFFLINE")
        self.lbl_stream_status.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px;")
        status_bar.addWidget(self.lbl_stream_status)

        status_bar.addStretch()

        self.lbl_event_age = QLabel("Last event: -")
        self.lbl_event_age.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px;")
        status_bar.addWidget(self.lbl_event_age)

        main_layout.addLayout(status_bar)

        # Keyboard shortcuts
        QShortcut(QKeySequence("F5"), self).activated.connect(self._force_refresh)

        # Composer revision: bump on USER edits only (textEdited never fires
        # for programmatic setText/clear), so a stale completion can never
        # destroy newer user text.
        self.input_prompt.textEdited.connect(self._on_composer_edited)

    # ── Worker Threads ──────────────────────────────────────────────

    def _start_workers(self):
        # Observer thread
        self._observer = obs.ReadOnlyObserver(self.db_path)
        self._observer_thread = QThread(self)
        self._observer.moveToThread(self._observer_thread)
        # Connect queued commands ONLY AFTER moveToThread.
        self._observer_thread.started.connect(self._observer.start)
        self._observer.select_session.connect(self._observer._on_select_session)
        self._observer.apply_snapshots.connect(self._observer._on_snapshots)
        self._observer.refresh_now.connect(self._observer._on_refresh_now)
        self._observer.stop_worker.connect(self._observer._on_stop)

        self._observer.signals.sidebar_updated.connect(self._on_sidebar_updated)
        self._observer.signals.pending_updated.connect(self._on_pending_updated)
        self._observer.signals.live_text_updated.connect(self._on_live_text_updated)
        self._observer.signals.activity_changed.connect(self._on_activity_changed)
        self._observer.signals.event_observed.connect(self._on_event_observed)
        self._observer.signals.observer_health.connect(self._on_observer_health)
        self._observer.signals.error.connect(self._on_observer_error)

        self._observer_thread.start()

        # Endpoint resolver thread (generation-aware)
        self._resolver = ep.OpenCodeEndpointResolver(
            generation_provider=lambda: self._resolution_generation)
        self._resolver_thread = QThread(self)
        self._resolver.moveToThread(self._resolver_thread)
        self._resolver_thread.started.connect(self._resolver.start)
        self._resolver.select_session.connect(self._resolver._on_select_session)
        self._resolver.resolve_now.connect(self._resolver._on_resolve_now)
        self._resolver.stop_worker.connect(self._resolver._on_stop)
        self._resolver.endpoint_resolved.connect(self._on_endpoint_resolved)
        # Feed verified process snapshots to the observer (queued).
        self._resolver.process_snapshots.connect(self._observer.apply_snapshots)

        self._resolver_thread.start()

        # Sender thread
        self._sender = sender_mod.NativeSender()
        self._sender_thread = QThread(self)
        self._sender.moveToThread(self._sender_thread)
        self._sender.send_prompt_cmd.connect(self._sender._on_send_prompt)
        self._sender.send_audit_cmd.connect(self._sender._on_send_audit)
        self._sender.send_three_waves_cmd.connect(self._sender._on_send_3_waves)
        self._sender.send_verified_cmd.connect(self._sender._on_send_verified)
        self._sender.stop_worker.connect(self._sender._on_stop)

        self._sender.signals.send_started.connect(self._on_send_started)
        self._sender.signals.send_completed.connect(self._on_send_completed)
        self._sender.signals.waves_progress.connect(self._on_waves_progress)
        self._sender.signals.waves_completed.connect(self._on_waves_completed)
        self._sender.signals.waves_failed.connect(self._on_waves_failed)

        self._sender_thread.start()

        # Live native SSE stream thread (ONE connection per endpoint).
        self._stream = lstream.LiveStreamWorker("")
        self._stream_thread = QThread(self)
        self._stream.moveToThread(self._stream_thread)
        self._stream_thread.started.connect(self._stream.start)
        self._stream.select_session.connect(self._stream._on_select_session)
        self._stream.retarget_endpoint.connect(self._stream._on_retarget_endpoint)
        self._stream.refresh_now.connect(self._stream._on_refresh)
        self._stream.stop_worker.connect(self._stream._on_stop)

        self._stream.snapshot_ready.connect(self._on_live_snapshot)
        self._stream.live_event_time.connect(self._on_live_event_time)
        # P0: consume live_event in production — lifecycle-significant SSE
        # events feed execution/activity state through this queued path.
        self._stream.live_event.connect(self._on_live_event)
        self._stream.stream_status.connect(self._on_stream_status)

        self._stream_thread.start()

        # Preset/file worker thread (all preset + audit-file I/O)
        self._preset_worker = PresetWorker()
        self._preset_thread = QThread(self)
        self._preset_worker.moveToThread(self._preset_thread)
        self._preset_thread.started.connect(self._preset_worker.start)
        self._preset_worker.refresh_requested.connect(self._preset_worker.refresh_presets)
        self._preset_worker.save_requested.connect(self._preset_worker.save_preset)
        self._preset_worker.read_requested.connect(self._preset_worker.read_audit_file)
        self._preset_worker.path_requested.connect(self._preset_worker.preset_path)
        self._preset_worker.stop_requested.connect(self._preset_worker._on_stop)
        self._preset_worker.presets_loaded.connect(self._on_presets_loaded)
        self._preset_worker.preset_saved.connect(self._on_preset_saved)
        self._preset_worker.audit_file_loaded.connect(self._on_audit_file_loaded)
        self._preset_worker.file_error.connect(self._on_file_error)
        self._preset_worker.stopped.connect(self._preset_thread.quit)

        self._preset_thread.start()

        # Health UI timer (runs on main thread, only updates labels)
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._update_health_display)
        self._health_timer.start(1000)

    # ── Session Selection ───────────────────────────────────────────

    def _on_session_selected(self, row: int):
        if row < 0 or row >= len(self.sessions):
            self.current_session_id = None
            self._update_header_no_session()
            self._update_send_controls()
            return

        selected = self.sessions[row]
        self.current_session_id = selected.id
        self._pinned_session_row = row
        self._session_unavailable = False
        # New selection = new resolution generation. Any resolver result
        # computed for an older generation is discarded as stale, and the
        # PREVIOUS selection's endpoint authority is invalidated at once —
        # session B never inherits session A's resolution.
        self._resolution_generation += 1
        self._endpoint_status = ep.EndpointStatus(
            "UNAVAILABLE", None, "Resolving endpoint for selection...")

        self.lbl_session_name.setText(selected.project_name)
        short_dir = selected.directory
        if len(short_dir) > 50:
            short_dir = "..." + short_dir[-47:]
        self.lbl_session_dir.setText(short_dir)
        self.lbl_session_name.setStyleSheet(f"color: {C_BORDER_HL}; font-weight: bold; font-size: 12px;")
        self.lbl_native_status.setStyleSheet(f"color: {C_TEXT_SECONDARY}; font-weight: bold;")

        # Tell observer to track this session (queued command)
        self._observer.select_session.emit(selected.id)
        # Tell resolver to verify endpoint for this session (queued command)
        self._resolver.select_session.emit(selected.id)
        # Scope the live SSE projection to this exact session (queued)
        self._stream.select_session.emit(selected.id)

        # Presentation authority: invalidate ALL old-session state so a
        # reconnect can never resurrect the previous session's text.
        self._presenter.set_session(selected.id)
        # Clear live output for fresh session (presenter is now empty)
        self.txt_live_output.clear()
        self.lbl_activity.setText("")

        self._update_send_controls()

    def _update_header_no_session(self):
        self.lbl_session_name.setText("No session selected")
        self.lbl_session_dir.setText("Select a session from the sidebar")
        self.lbl_queue_count.setText("Q: 0")
        self.lbl_native_status.setText("[—]")
        self._presenter.set_session(None)
        self.txt_live_output.clear()
        self.lbl_activity.setText("")

    # ── Observer Signal Handlers ────────────────────────────────────

    @pyqtSlot(list)
    def _on_sidebar_updated(self, summaries: list):
        """Receive immutable session summary list from observer worker."""
        self.sessions = summaries

        # Sort: INPUT REQUIRED > FAILED > RUNNING > queued > IDLE
        def sort_key(s):
            base = _STATE_SORT_ORDER.get(s.state, 3)
            # Sessions with pending queue items sort higher within IDLE
            if s.state == "IDLE" and s.pending_count > 0:
                base = 2  # Same as RUNNING
            return (base, -s.time_updated)

        sorted_sessions = sorted(summaries, key=sort_key)

        # Selection stability: freeze the selected session's visual position
        # while it remains selected; other rows reorder around it.
        if self.current_session_id:
            selected = next(
                (s for s in sorted_sessions if s.id == self.current_session_id), None)
            if selected is not None:
                rest = [s for s in sorted_sessions if s.id != self.current_session_id]
                sorted_sessions = [selected] + rest
        self.sessions = sorted_sessions

        # Rebuild sidebar, preserving selection
        self.session_list.blockSignals(True)
        self.session_list.clear()

        target_row = -1
        for i, s in enumerate(sorted_sessions):
            p_badge = f" [{s.pending_count}]" if s.pending_count > 0 else ""

            if s.state == "RUNNING":
                st_tag = "●"
            elif s.state == "FAILED":
                st_tag = "✖"
            elif s.state == "NEEDS_HUMAN":
                st_tag = "■"
            else:
                st_tag = "○"

            txt = f"{st_tag} {s.project_name}{p_badge}"
            it = QListWidgetItem(txt)
            it.setData(Qt.ItemDataRole.UserRole, s.id)
            self.session_list.addItem(it)

            if self.current_session_id and s.id == self.current_session_id:
                target_row = i

        if target_row >= 0:
            self.session_list.setCurrentRow(target_row)
        elif self.current_session_id:
            # Session disappeared — do NOT retarget. Fail closed NOW.
            self._on_session_disappeared()

        self.session_list.blockSignals(False)

        # Update queue count in header. The selected session's STATE goes
        # through the lifecycle authority (Target B): a durable snapshot
        # carries its ordering witness (latest_event_seq) so an OLDER
        # durable snapshot can never regress a newer live state; a
        # matching/newer durable snapshot reconciles the live overlay.
        if self.current_session_id and not self._session_unavailable:
            cur = next((s for s in sorted_sessions if s.id == self.current_session_id), None)
            if cur:
                self.lbl_queue_count.setText(f"Q: {cur.pending_count}")
                self._presenter.on_durable_lifecycle(
                    self.current_session_id, cur.state, cur.detail,
                    seq=cur.latest_event_seq)
                self._update_native_status(self._presenter.lifecycle_state or cur.state)

    def _on_session_disappeared(self):
        """Immediate fail-closed on selected-session disappearance.

        Marks unavailable, disables every send/audit control, invalidates
        endpoint authorization, clears pending display, and stops live-tail
        ownership. Does NOT wait for a resolver tick and does NOT auto-select
        another session.
        """
        self._session_unavailable = True
        self._endpoint_status = ep.EndpointStatus(
            "UNAVAILABLE", None, "Selected session no longer available")
        self.current_queue = []
        self.table_queue.setRowCount(0)
        self.lbl_queue_count.setText("Q: 0")
        self.lbl_session_name.setText("SESSION NO LONGER AVAILABLE")
        self.lbl_session_dir.setText(f"ID: {(self.current_session_id or '')[:20]}...")
        self.lbl_native_status.setText("[GONE]")
        self.lbl_native_status.setStyleSheet(f"color: {C_DANGER_TEXT}; font-weight: bold;")
        # Stop live-tail ownership for the missing session.
        self._observer.select_session.emit("")
        self._stream.select_session.emit("")
        self._presenter.set_session(None)
        self._update_send_controls()

    def _update_native_status(self, state: str):
        """PRODUCTION lifecycle display (Target B): the presenter's
        arbitrated execution state drives the selected-session header
        status directly — no test-only listener, no SQLite mutation."""
        if not self.current_session_id or self._session_unavailable:
            return
        color = C_ACCENT_TEAL
        if state == "RUNNING":
            color = C_BORDER_HL
        elif state == "FAILED":
            color = C_DANGER_TEXT
        elif state == "NEEDS_HUMAN":
            color = "#FF8C2E"

        self.lbl_native_status.setText(f"[{state}]")
        self.lbl_native_status.setStyleSheet(f"color: {color}; font-weight: bold;")

    # ── Presentation Authority outputs ─────────────────────────────

    @pyqtSlot(str)
    def _on_presented_text(self, text: str):
        """The ONLY path that writes LIVE OUTPUT. The presenter has already
        arbitrated durable-vs-live authority."""
        self.txt_live_output.setPlainText(text)
        scrollbar = self.txt_live_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @pyqtSlot(str)
    def _on_presented_activity(self, activity: str):
        self.lbl_activity.setText(activity)

    @pyqtSlot(str)
    def _on_presented_state(self, state: str):
        """PRODUCTION lifecycle display (Target B): the presenter's
        arbitrated execution state drives the selected-session header
        status directly — no test-only listener, no SQLite mutation."""
        if not self.current_session_id or self._session_unavailable:
            return
        self._update_native_status(state)

    def _on_live_text_updated(self, session_id: str, snapshot) -> None:
        """Durable observer output enters the PRESENTATION OWNER (never the
        QTextEdit directly).

        Target A1: the payload is the IMMUTABLE DurableTextSnapshot
        captured atomically in the observer worker. The GUI never reads
        mutable worker internals to reconstruct event identity — a queued
        signal for generation A can never be paired with generation B's
        identity."""
        if isinstance(snapshot, present.DurableTextSnapshot):
            self._presenter.on_durable_text(session_id, snapshot.assembled_text,
                                            snapshot=snapshot)
        else:
            # Compatibility: a bare str payload carries no durable identity;
            # it can only refresh the current generation's text.
            self._presenter.on_durable_text(session_id, str(snapshot))

    @pyqtSlot(str, str)
    def _on_activity_changed(self, session_id: str, activity: str):
        """Receive compact activity line from observer (semantically
        separate from the Last event age, which uses event_observed)."""
        if session_id != self.current_session_id:
            return
        self._last_activity_line = activity
        self.lbl_activity.setText(activity)

    @pyqtSlot(str, int, float)
    def _on_event_observed(self, session_id: str, seq: int, timestamp: float):
        """Every newly consumed durable event (including text.delta)
        refreshes the Last event age AND advances the lifecycle authority's
        durable-sequence barrier (Target B): a durable state snapshot at or
        below this barrier predates the live overlay's cause."""
        if session_id != self.current_session_id:
            return
        self._last_event_time = timestamp
        self._presenter.note_durable_seq(seq)
        # Durable progress consumed past the live anchor: reconcile the
        # buffered durable fold IMMEDIATELY instead of waiting for the
        # 50ms timer (publish_now is the production convergence path).
        self._presenter.publish_now()

    @pyqtSlot(str, float)
    def _on_live_event_time(self, session_id: str, ts: float):
        """Live SSE events feed the Last event age with the NATIVE event
        timestamp (never the observer-read time)."""
        if session_id != self.current_session_id:
            return
        if ts > self._last_event_time:
            self._last_event_time = ts

    @pyqtSlot(str, str, object)
    def _on_live_event(self, session_id: str, event_type: str, data):
        """SSE live events enter the PRESENTATION OWNER through a queued,
        thread-safe path. The presenter arbitrates text authority and feeds
        lifecycle immediacy (activity/state) without ever touching SQLite."""
        self._presenter.on_live_event(session_id, event_type, data or {})

    @pyqtSlot(object)
    def _on_live_snapshot(self, snap):
        """Legacy SSE snapshot path retained for signal compatibility, but
        it NO LONGER writes the QTextEdit directly: text presentation is
        owned exclusively by the presenter."""
        # SSE snapshots are live projections; live_event is the canonical presentation input.

    @pyqtSlot(str, str)
    def _on_stream_status(self, endpoint_url: str, status: str):
        if (status == "OFFLINE" or not endpoint_url
                or endpoint_url == self._stream_endpoint):
            self._stream_status = status

    @pyqtSlot(str, float)
    def _on_observer_health(self, status: str, age: float):
        self._observer_status = status
        self._observer_age = age

    @pyqtSlot(str)
    def _on_observer_error(self, msg: str):
        self.lbl_feedback.setText(f"Observer: {msg}")

    # ── Endpoint Signal Handlers ────────────────────────────────────

    @pyqtSlot(object)
    def _on_endpoint_resolved(self, status):
        """Receive endpoint resolution result.

        STALE-RESULT GATE: a resolution is accepted only when it belongs to
        the CURRENT resolver generation AND targets the CURRENT session. A
        result computed for session A after the GUI selected session B is
        discarded — it can never authorize a send.
        """
        if (self.current_session_id is None
                or status.generation != self._resolution_generation
                or status.session_id != self.current_session_id):
            return  # stale: discard, keep the previous authoritative status
        self._endpoint_status = status
        # Keep the presentation authority scoped to the accepted selection
        # (covers selection paths that set the session without a click).
        self._presenter.set_session(self.current_session_id)
        if status.state == "RESOLVED" and status.endpoint is not None:
            url = status.endpoint.url
            if url != self._stream_endpoint:
                self._stream_endpoint = url
                self._stream.retarget_endpoint.emit(url)
            # Scope the live stream to the exact selected session.
            self._stream.select_session.emit(self.current_session_id)
        self._update_send_controls()

    # ── Sender Signal Handlers ──────────────────────────────────────

    @staticmethod
    def _kind_label(kind: Optional[str]) -> str:
        return "SAIPEN" if kind in ("prompt", "cc") else "SAIPAL"

    @pyqtSlot(object)
    def _on_send_started(self, envelope: dict):
        """Target C1: session/request ownership gating BEFORE any
        selected-session UI mutation.

        An old-session send_started may update INTERNAL bookkeeping for its
        own request (inflight entry, its own admission reservation) but
        must NEVER mutate the selected session's feedback, composer,
        controls, presentation or execution state. Otherwise a delayed A
        start after a switch to B would show A's 'sending...' on B and
        leave B's controls stuck until an unrelated refresh."""
        rid = envelope.get("request_id")
        owner = self._admission_owner
        stale = (envelope.get("session_id") is not None
                 and envelope.get("session_id") != self.current_session_id)

        # Internal bookkeeping for the request's OWN identity.
        if rid not in self._inflight_sends:
            self._inflight_sends[rid] = {
                "composer": self._composer_submit if not stale else None,
                "session_id": envelope.get("session_id"),
                "kind": envelope.get("kind"),
            }
        if not stale:
            self._composer_submit = None
            # If the reservation was NOT minted by a GUI activation (e.g. a
            # legacy direct worker call), adopt it as the current owner so a
            # matching completion can release it.
            if (self._admission_owner is None
                    and rid
                    and envelope.get("gui_minted") is not True):
                self._admission_owner = {
                    "path": "worker", "request_id": rid,
                    "session_id": envelope.get("session_id"), "batch": False,
                }
                owner = self._admission_owner

        if stale:
            # Old-session start: bookkeeping only, then recompute the
            # selected session's controls from B's OWN authority so a
            # released stale reservation can never leave them stuck.
            self._update_send_controls()
            return

        kind = envelope.get("kind")
        label = self._kind_label(kind)
        if kind == "cc":
            self.lbl_feedback.setText("SAIPEN: sending cc...")
        elif kind in ("prompt",):
            self.lbl_feedback.setText("SAIPEN: sending...")
        else:
            self.lbl_feedback.setText(f"SAIPAL: sending {str(kind).upper()}...")
        self._update_send_controls()

    @pyqtSlot(object)
    def _on_send_completed(self, result):
        entry = self._inflight_sends.pop(result.request_id, None)
        released = self._release_admission(result.request_id)
        stale = (result.session_id is not None
                 and result.session_id != self.current_session_id)
        if released and stale:
            # Target C1: after releasing a stale matching admission
            # reservation, recompute the selected session's controls from
            # its OWN authority (B must be usable again without waiting
            # for an unrelated refresh/resolver tick).
            self._update_send_controls()
        label = self._kind_label(result.kind)

        # STALE COMPLETION RULE: a result belonging to a no-longer-selected
        # session must not mutate the selected session's feedback, composer,
        # controls, admission ownership or presentation state. Internal
        # bookkeeping above (inflight entry removal, ownership release for
        # the matching request_id) is safe; ALL selected-session UI is
        # skipped.
        if stale:
            return

        if result.success:
            slot_names = {"core": "CORE", "wave2": "W2", "performance": "PERF",
                          "audit-file": "FILE"}
            kind_txt = slot_names.get(result.kind)
            if kind_txt:
                self.lbl_feedback.setText(
                    f"{label}: {kind_txt} queued #{result.admitted_seq}")
            else:
                self.lbl_feedback.setText(f"{label}: queued #{result.admitted_seq}")
        else:
            self.lbl_feedback.setText(f"{label}: failed: {result.error}")

        # Composer protection: clear ONLY the exact submitted text when the
        # user has not edited the composer since submission. Newer user
        # text is never destroyed by an older completion.
        if result.success and entry and entry.get("composer"):
            comp = entry["composer"]
            if (comp.get("rev") == self._composer_rev
                    and self.input_prompt.text() == comp.get("text")):
                self.input_prompt.clear()
        self._update_send_controls()

    @pyqtSlot(object)
    def _on_waves_progress(self, info: dict):
        if info.get("session_id") != self.current_session_id:
            return  # stale wave progress for an older selection
        self.lbl_feedback.setText(info.get("message", ""))

    @pyqtSlot(object)
    def _on_waves_completed(self, result):
        self._release_admission(result.request_id)
        if result.session_id != self.current_session_id:
            return
        self.lbl_feedback.setText(f"SAIPAL: CORE/W2/PERF queued ({result.request_id})")
        self._update_send_controls()

    @pyqtSlot(object)
    def _on_waves_failed(self, result):
        self._release_admission(result.request_id)
        if result.session_id != self.current_session_id:
            return
        self.lbl_feedback.setText(f"SAIPAL: wave failed: {result.error}")
        self._update_send_controls()

    # ── Preset Worker Handlers ──────────────────────────────────────

    @pyqtSlot(object)
    def _on_presets_loaded(self, paths: dict):
        self._preset_paths = paths
        btn_map = {"core": self.btn_core, "wave2": self.btn_w2, "performance": self.btn_perf}
        for slot, btn in btn_map.items():
            path = paths.get(slot)
            if path:
                btn.setToolTip(f"Preset: {os.path.basename(path)}\nRight-click to change")
            else:
                btn.setToolTip("Not configured. Click to select file.\nRight-click to change")

    @pyqtSlot(str, str)
    def _on_preset_saved(self, slot: str, path: str):
        self.lbl_feedback.setText(f"Preset {slot}: {os.path.basename(path)}")
        if self._pending_audit_slot == slot:
            self._pending_audit_slot = None
            self._send_audit(slot)  # re-acquires admission ownership

    @pyqtSlot(object, str)
    def _on_audit_file_loaded(self, request, text: str):
        """The immutable envelope captured at click time is the ONLY target.

        Even if the user switched sidebar selection while the file was being
        read, this completion sends to the CAPTURED session on the CAPTURED
        endpoint (the sender revalidates that authority before POSTing) —
        never to the currently selected session.
        """
        self._sender.send_verified_cmd.emit(request, text)

    @pyqtSlot(str, object)
    def _on_file_error(self, msg: str, request):
        # Release the MATCHING reservation: an async file/preset failure is
        # a terminal path and must never strand GUI admission ownership.
        if request is not None:
            self._release_admission(getattr(request, "request_id", None))
        self.lbl_feedback.setText(msg)
        self._pending_audit_slot = None

    # ── Send Controls State ─────────────────────────────────────────

    def _update_send_controls(self):
        """Enable/disable send controls — EXACTLY the fail-closed
        ownership verdict of _can_send(), never a weaker duplicate.

        Target C2: while exclusive GUI admission ownership exists, the
        visible state MUST match the ownership policy — controls show an
        explicit busy state and are never apparently enabled while every
        activation would be silently rejected by the owner. After the
        exact matching owner releases, the same recomputation restores
        them (no unrelated resolver/sidebar tick needed)."""
        can_send = self._can_send()
        owned = self._admission_owner is not None
        busy = owned or not can_send
        self.btn_send.setEnabled(can_send and not owned)
        self.btn_cc.setEnabled(can_send and not owned)
        self.btn_core.setEnabled(can_send and not owned)
        self.btn_w2.setEnabled(can_send and not owned)
        self.btn_perf.setEnabled(can_send and not owned)
        self.btn_3waves.setEnabled(can_send and not owned)
        self.btn_audit_file.setEnabled(can_send and not owned)
        self.input_prompt.setEnabled(can_send and not owned)

        if not self.current_session_id:
            self.input_prompt.setPlaceholderText("Select a session from the sidebar...")
        elif self._session_unavailable:
            self.input_prompt.setPlaceholderText("SESSION UNAVAILABLE — sending disabled")
        elif self._endpoint_status.state == "AMBIGUOUS":
            self.input_prompt.setPlaceholderText("AMBIGUOUS OPENCODE ENDPOINT — cannot send")
        elif owned:
            # Explicit visible busy state for GUI admission ownership.
            path = self._admission_owner.get("path") or "request"
            batch = " (CORE/W2/PERF)" if self._admission_owner.get("batch") else ""
            self.input_prompt.setPlaceholderText(
                f"Admission in flight{batch} ({path}) — waiting for result...")
        elif self._endpoint_status.state != "RESOLVED":
            self.input_prompt.setPlaceholderText("Waiting for OpenCode endpoint...")
        else:
            cur = next((s for s in self.sessions if s.id == self.current_session_id), None)
            name = cur.project_name if cur else "session"
            self.input_prompt.setPlaceholderText(f"Queue prompt into {name}...")

    # ── Actions ─────────────────────────────────────────────────────

    def _build_request(self, kind: str, slot: str = "", path: str = "") -> AuditRequest:
        """Capture the COMPLETE send target at click time (immutable)."""
        es = self._endpoint_status
        cur = next((s for s in self.sessions
                    if s.id == self.current_session_id), None)
        return AuditRequest(
            request_id=sender_mod.NativeSender.new_request_id(),
            kind=kind,
            session_id=self.current_session_id or "",
            project_name=cur.project_name if cur else "",
            resolver_generation=self._resolution_generation,
            endpoint_pid=es.endpoint.pid if es.endpoint else None,
            endpoint_start_time=es.endpoint.start_time if es.endpoint else None,
            endpoint_url=es.endpoint.url if es.endpoint else None,
            slot=slot,
            path=path,
        )

    def _on_composer_edited(self, _text: str):
        self._composer_rev += 1

    # ── GUI-side single-flight admission ownership ──────────────────

    def _acquire_admission(self, path: str) -> Optional[dict]:
        """Mint request/batch identity SYNCHRONOUSLY at USER ACTIVATION and
        acquire GUI-side ownership BEFORE any emit. Returns the owner dict,
        or None when the matching admission path is already owned (the
        second activation of a double-click enqueues NOTHING).

        Not a global boolean: ownership is per-path and released only by
        the matching request_id/batch_id on every terminal path.
        """
        if self._admission_owner is not None:
            if self._admission_owner.get("path") == path:
                return None  # same path already owned: single-flight
            # A different path while one is in flight is also refused:
            # ownership is exclusive.
            return None
        rid = sender_mod.NativeSender.new_request_id()
        self._admission_owner = {
            "path": path,
            "request_id": rid,
            "session_id": self.current_session_id,
            "batch": path == "3waves",
        }
        return self._admission_owner

    def _release_admission(self, request_id: Optional[str]) -> bool:
        """Release ownership ONLY for the matching request_id. Stale or
        newer completions never release a newer reservation."""
        owner = self._admission_owner
        if owner is not None and owner.get("request_id") == request_id:
            self._admission_owner = None
            return True
        return False

    def _on_send(self):
        owner = self._acquire_admission("send")
        if owner is None:
            return
        if not self._can_send():
            self._release_admission(owner["request_id"])
            return
        text = self.input_prompt.text().strip()
        if not text:
            self._release_admission(owner["request_id"])
            return
        self._composer_submit = {"text": text, "rev": self._composer_rev}
        ep_obj = self._endpoint_status.endpoint
        self._sender.send_prompt_cmd.emit(
            ep_obj.url, self.current_session_id, text, owner["request_id"])

    def _on_cc(self):
        owner = self._acquire_admission("cc")
        if owner is None:
            return
        if not self._can_send():
            self._release_admission(owner["request_id"])
            return
        ep_obj = self._endpoint_status.endpoint
        self._sender.send_prompt_cmd.emit(
            ep_obj.url, self.current_session_id, "cc", owner["request_id"])

    def _on_core(self):
        self._on_audit("core")

    def _on_w2(self):
        self._on_audit("wave2")

    def _on_perf(self):
        self._on_audit("performance")

    def _on_audit(self, slot: str):
        """Admission ownership begins at USER ACTIVATION — never after the
        async preset/file read completes."""
        owner = self._acquire_admission(slot)
        if owner is None:
            return
        if not self._can_send():
            self._release_admission(owner["request_id"])
            return
        path = self._preset_paths.get(slot)
        if not path:
            self._release_admission(owner["request_id"])
            self._pending_audit_slot = slot
            self._on_change_preset(slot)
            return
        self._send_audit(slot, owner)

    def _send_audit(self, slot: str, owner: Optional[dict] = None):
        """Preset TEXT reading happens in the worker via an immutable
        request envelope; the captured target never changes."""
        path = self._preset_paths.get(slot)
        if not path:
            self.lbl_feedback.setText(f"Preset not configured: {slot}")
            if owner:
                self._release_admission(owner["request_id"])
            return
        if not self._can_send():
            if owner:
                self._release_admission(owner["request_id"])
            return
        if owner is None:
            owner = self._acquire_admission(slot)
            if owner is None:
                return
        request = self._build_request(kind=slot, slot=slot, path=path)
        # The reservation travels with the immutable envelope so the file
        # error/success path releases the MATCHING owner.
        request = replace(request,
                          request_id=owner["request_id"])
        self.lbl_feedback.setText(f"SAIPAL: sending {slot.upper()}...")
        self._preset_worker.read_requested.emit(request)

    def _on_3waves(self):
        """ONE reservation owns the complete 3-wave batch: a second click
        while the batch is owned enqueues NOTHING (double-click of 3 WAVES
        produces exactly CORE/W2/PERF once)."""
        owner = self._acquire_admission("3waves")
        if owner is None:
            return
        if not self._can_send():
            self._release_admission(owner["request_id"])
            return
        # Validate all three presets exist (cached worker state, no I/O)
        missing = []
        for slot_name, label in [("core", "CORE"), ("wave2", "W2"), ("performance", "PERF")]:
            if not self._preset_paths.get(slot_name):
                missing.append(label)
        if missing:
            self._release_admission(owner["request_id"])
            self.lbl_feedback.setText(f"Missing presets: {', '.join(missing)}")
            return
        ep_obj = self._endpoint_status.endpoint
        # Endpoint + session are captured HERE as immutable emit arguments;
        # the sender worker reads preset files and POSTs to exactly this
        # captured target with the single batch request_id minted at
        # activation.
        self._sender.send_three_waves_cmd.emit(
            ep_obj.url, self.current_session_id, owner["request_id"])

    def _on_audit_file(self):
        """Ownership begins at USER ACTIVATION — before the async file
        dialog/reading completes."""
        owner = self._acquire_admission("audit-file")
        if owner is None:
            return
        if not self._can_send():
            self._release_admission(owner["request_id"])
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Select audit file", "",
            "Text files (*.md *.txt);;All files (*)"
        )
        if not path:
            self._release_admission(owner["request_id"])
            return
        # Immutable envelope captured at click time; all filesystem
        # validation/read runs in the worker thread. The reservation
        # travels with the envelope so a file error releases the MATCHING
        # owner.
        request = self._build_request(kind="audit-file", slot="", path=path)
        request = replace(request, request_id=owner["request_id"])
        self.lbl_feedback.setText("SAIPAL: sending FILE...")
        self._preset_worker.read_requested.emit(request)

    def _on_change_preset(self, slot: str):
        """Open file picker to configure a preset slot."""
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {slot} audit file", "",
            "Text files (*.md *.txt);;All files (*)"
        )
        if path:
            # Persistence I/O runs in the worker thread.
            self._preset_worker.save_requested.emit(slot, path)
        else:
            # Preset-change cancelled: release any pending pending-audit.
            self._pending_audit_slot = None

    def _can_send(self) -> bool:
        """Fail-closed redundant ownership check.

        Requires ALL of:
        - a current session that has not disappeared;
        - endpoint status RESOLVED for the CURRENT resolver generation;
        - the result targets exactly the current session;
        - the current session is in the endpoint's verified session authority;
        - the session still exists in the latest observer summaries.
        A generic RESOLVED boolean is insufficient; no send may rely on the
        last endpoint object from another selection.
        """
        es = self._endpoint_status
        return (
            self.current_session_id is not None
            and not self._session_unavailable
            and es.state == "RESOLVED"
            and es.endpoint is not None
            and es.generation == self._resolution_generation
            and es.session_id == self.current_session_id
            and self.current_session_id in (es.endpoint.sessions or ())
            and any(s.id == self.current_session_id for s in self.sessions)
        )

    # ── Queue Table ─────────────────────────────────────────────────

    def _refresh_queue_table(self):
        """Clear queue table when no session selected."""
        if not self.current_session_id:
            self.table_queue.setRowCount(0)
            self.current_queue = []

    @pyqtSlot(str, list)
    def _on_pending_updated(self, session_id: str, items: list):
        """Receive pending queue items from observer.

        ALWAYS emitted by the observer for the selected session — including
        an empty list — so the table clears immediately when the durable
        native queue drains to zero (N -> 0).
        """
        if session_id != self.current_session_id:
            return
        self.current_queue = items
        now_ms = time.time() * 1000

        self.table_queue.setRowCount(len(items))
        for r, item in enumerate(items):
            it_num = QTableWidgetItem(f"#{r+1}")
            it_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            snippet = item.prompt_text.replace("\r\n", " ").replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:117] + "..."
            it_text = QTableWidgetItem(snippet)

            age_sec = max(0, int((now_ms - item.time_created) / 1000))
            age_str = f"{age_sec}s" if age_sec < 60 else f"{age_sec // 60}m"
            it_time = QTableWidgetItem(age_str)
            it_time.setForeground(QColor(C_TEXT_MUTED))
            it_time.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self.table_queue.setItem(r, 0, it_num)
            self.table_queue.setItem(r, 1, it_text)
            self.table_queue.setItem(r, 2, it_time)

        self.lbl_queue_count.setText(f"Q: {len(items)}")

    # ── Health Display ──────────────────────────────────────────────

    def _update_health_display(self):
        """Update status bar labels. Runs on main thread via 1s timer."""
        # DB health
        if self._observer_status == "LIVE":
            self.lbl_db_health.setText(f"DB: LIVE {self._observer_age:.1f}s")
            self.lbl_db_health.setStyleSheet(f"color: {C_SUCCESS}; font-size: 10px;")
        elif self._observer_status == "BUSY":
            self.lbl_db_health.setText(f"DB: BUSY {self._observer_age:.1f}s")
            self.lbl_db_health.setStyleSheet(f"color: #FF8C2E; font-size: 10px;")
        else:
            self.lbl_db_health.setText(f"DB: ERROR")
            self.lbl_db_health.setStyleSheet(f"color: {C_DANGER_TEXT}; font-size: 10px;")

        # OpenCode endpoint health
        es = self._endpoint_status
        if es.state == "RESOLVED" and es.endpoint:
            self.lbl_oc_health.setText(f"OC: LIVE pid:{es.endpoint.pid}")
            self.lbl_oc_health.setStyleSheet(f"color: {C_SUCCESS}; font-size: 10px;")
        elif es.state == "AMBIGUOUS":
            self.lbl_oc_health.setText("OC: AMBIGUOUS")
            self.lbl_oc_health.setStyleSheet(f"color: #FF8C2E; font-size: 10px;")
        else:
            self.lbl_oc_health.setText("OC: OFFLINE")
            self.lbl_oc_health.setStyleSheet(f"color: {C_DANGER_TEXT}; font-size: 10px;")

        # STREAM transport truth — semantically separate from DB/OC health
        # and from execution state (RUNNING + STREAM: RECONNECTING means
        # OpenCode is alive but realtime token projection is degraded; it
        # is NOT IDLE merely because no token arrived recently).
        if es.state != "RESOLVED" or not self.current_session_id:
            stream_txt = "OFFLINE"
        elif self._stream_status == "LIVE":
            stream_txt = "LIVE"
        elif self._stream_status == "RECONNECTING":
            stream_txt = "RECONNECTING"
        else:
            stream_txt = "DB-ONLY"
        self.lbl_stream_status.setText(f"STREAM: {stream_txt}")
        if stream_txt == "LIVE":
            self.lbl_stream_status.setStyleSheet(f"color: {C_SUCCESS}; font-size: 10px;")
        elif stream_txt == "RECONNECTING":
            self.lbl_stream_status.setStyleSheet(f"color: #FF8C2E; font-size: 10px;")
        elif stream_txt == "DB-ONLY":
            self.lbl_stream_status.setStyleSheet(f"color: {C_TEXT_SECONDARY}; font-size: 10px;")
        else:
            self.lbl_stream_status.setStyleSheet(f"color: {C_DANGER_TEXT}; font-size: 10px;")

        # Last event age — fed by event_observed (EVERY durable event,
        # including text.delta) with NATIVE event timestamps, and by the
        # live SSE stream's native timestamps. Never poll age.
        if self._last_event_time > 0:
            age = time.time() - self._last_event_time
            age_str = f"{age:.0f}s" if age < 60 else f"{age / 60:.0f}m"
            self.lbl_event_age.setText(f"Last event: {age_str} ago")
        else:
            self.lbl_event_age.setText("Last event: -")

    # ── Pin ──────────────────────────────────────────────────────────

    def _toggle_pin(self, checked: bool):
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    # ── Force Refresh ────────────────────────────────────────────────

    def _force_refresh(self):
        """F5 means what the UI claims: force observer refresh, endpoint
        resolution, the live stream reconnect, AND the preset refresh."""
        self.lbl_feedback.setText("Refreshing...")
        self._observer.refresh_now.emit()
        self._resolver.resolve_now.emit()
        self._stream.refresh_now.emit()
        self._preset_worker.refresh_requested.emit()

    # ── Cleanup ──────────────────────────────────────────────────────

    def _shutdown_workers(self):
        """Clean shutdown sequence (queued, never direct worker calls):

        1. disable Viewer controls;
        2. emit queued stop commands;
        3. each worker stops its own QTimer / closes its own resources
           in its own thread;
        4. worker emits stopped (observer/resolver);
        5. quit that QThread;
        6. bounded wait;
        7. close Viewer.
        """
        if self._closing:
            return
        self._closing = True

        # 1. Disable controls. GUI-side admission ownership is released
        # on EVERY terminal path — closing the Viewer is terminal: no
        # stranded reservation may survive shutdown.
        self._health_timer.stop()
        for w in (self.btn_send, self.btn_cc, self.btn_core, self.btn_w2,
                  self.btn_perf, self.btn_3waves, self.btn_audit_file):
            w.setEnabled(False)
        self.input_prompt.setEnabled(False)
        self._admission_owner = None
        # Presentation authority stops publishing: no signal may reach a
        # closing window (presenter timer stopped, emissions suppressed).
        self._presenter.stop()

        # 2. Queued stop commands. The live stream worker aborts its own
        # QNetworkReply from its OWN thread — closing the Viewer cancels
        # Viewer-side observation/admission UI work but NEVER sends any
        # OpenCode abort/interrupt.
        self._observer.stop_worker.emit()
        self._resolver.stop_worker.emit()
        self._sender.stop_worker.emit()
        self._stream.stop_worker.emit()
        self._preset_worker.stop_requested.emit()

        # Let the queued stop commands be delivered and processed by each
        # worker in its own thread (bounded, event-loop based).
        from PyQt6.QtCore import QEventLoop
        loop = QEventLoop()
        QTimer.singleShot(500, loop.quit)
        loop.exec()

        # 5+6. Quit threads with bounded waits.
        threads = [
            (self._observer_thread, "observer"),
            (self._resolver_thread, "resolver"),
            (self._sender_thread, "sender"),
            (self._stream_thread, "stream"),
            (self._preset_thread, "preset"),
        ]
        for thread, name in threads:
            thread.quit()
            if not thread.wait(3000):
                print(f"[queue-viewer] WARNING: {name} thread did not stop "
                      f"within 3s (isRunning={thread.isRunning()})",
                      file=sys.stderr)

    def closeEvent(self, event):
        self._shutdown_workers()
        super().closeEvent(event)


# ── find_db_path (reuse logic from queue_core without importing it) ──

def find_db_path() -> str:
    """Locate opencode.db."""
    env = os.environ.get("OPENCODE_DB")
    if env and os.path.isfile(env):
        return env
    home = os.path.expanduser("~")
    p1 = os.path.join(home, ".local", "share", "opencode", "opencode.db")
    if os.path.isfile(p1):
        return p1
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        p2 = os.path.join(local, "opencode", "opencode.db")
        if os.path.isfile(p2):
            return p2
    return p1  # fallback


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="SAIPATCH Queue Viewer Cockpit")
    parser.add_argument("--session", "-s", help="Initial session ID")
    parser.add_argument("--db", help="Path to opencode.db")
    args = parser.parse_args()

    db_path = args.db or find_db_path()

    app = QApplication(sys.argv)

    # IRON LAW 1: Verdana, NON-ANTIALIASED everywhere
    font = QFont("Verdana", 9)
    font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    app.setFont(font)

    app.setStyleSheet(GOLDEN_DEFAULT_QSS)

    win = QueueViewerWindow(db_path=db_path, initial_session_id=args.session)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
