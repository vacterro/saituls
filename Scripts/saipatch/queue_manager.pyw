"""
queue_manager.pyw - Visual Queue Manager and Sidecar for OpenCode (SAIPATCH).
Provides transparency, live multi-session sidebar, unified queue, sent history,
instant reordering, direct editing, and zero-prompt instant dropping/clearing.
Strictly follows UI.md Golden Default (Win95 dark golden, Verdana NoAntialias, 2px bevels, zero white).
Built with PyQt6.

============================================================================
LEGACY DIRECT-DB TOOL -- NOT THE DEFAULT QUEUE VIEWER
============================================================================
This tool mutates the OpenCode SQLite database DIRECTLY for add, edit, drop,
reorder, clear and delete-session. It is NOT the supported Queue Viewer
surface. The normal GUI is queue_viewer.pyw (read-only observation + native
V2 delivery:"queue" admission), launched by queue.ps1 with no arguments.

DO NOT wire this module back as the default Viewer. Direct DB mutation can
corrupt native queue state and bypasses the native admission contract.
============================================================================
"""
import os
import sys
import json
import time
from datetime import datetime
import argparse

from PyQt6.QtCore import Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QFont, QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QLabel, QLineEdit, QTextEdit, QCheckBox,
    QFrame, QDialog, QAbstractItemView, QTabWidget
)

# Import sibling queue_core
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import queue_core

# UI.md Golden Default Palette
C_BG = "#1A1810"            # --background
C_BG_SOFT = "#232018"       # --backgroundSoft
C_SURFACE = "#332E22"       # --surface
C_SURFACE_RAISED = "#3D372A"# --surfaceRaised
C_SURFACE_ALT = "#453D30"   # --surfaceAlt
C_BORDER_DARK = "#100E08"   # --borderDark
C_BORDER_HL = "#F0D060"     # --borderHighlight
C_BEVEL_LIGHT = "#75663D"   # --bevelLight
C_BORDER_MUTED = "#5A5040"  # --borderMuted
C_TEXT_PRIMARY = "#D4C89A"  # --textPrimary
C_TEXT_SECONDARY = "#9C9371"# --textSecondary
C_TEXT_MUTED = "#6E674E"    # --textMuted
C_ACCENT_TEAL = "#008080"   # --accentTeal
C_DANGER = "#7A2020"        # --danger
C_DANGER_TEXT = "#D66464"   # --dangerText
C_SUCCESS = "#4A7A20"       # --success
C_SELECTION = "#3D372A"     # --selection

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

/* Tabs */
QTabWidget::pane {{
    border: 2px solid {C_BORDER_DARK};
    border-top-color: {C_BEVEL_LIGHT};
    border-left-color: {C_BEVEL_LIGHT};
    border-bottom-color: {C_BORDER_DARK};
    border-right-color: {C_BORDER_DARK};
    background-color: {C_BG_SOFT};
}}
QTabBar::tab {{
    background-color: {C_SURFACE};
    color: {C_TEXT_SECONDARY};
    border: 2px solid {C_BORDER_DARK};
    border-top-color: {C_BEVEL_LIGHT};
    border-left-color: {C_BEVEL_LIGHT};
    border-bottom: none;
    padding: 5px 12px;
    font-weight: bold;
}}
QTabBar::tab:selected {{
    background-color: {C_BG_SOFT};
    color: {C_BORDER_HL};
    border-bottom: 2px solid {C_BG_SOFT};
}}
QTabBar::tab:hover:!selected {{
    background-color: {C_SURFACE_RAISED};
    color: {C_TEXT_PRIMARY};
}}

/* Frames */
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

/* Sidebar List */
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

/* Tables */
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
QTableWidget::item:hover:!selected {{
    background-color: #2A251C;
}}

/* Header */
QHeaderView::section {{
    background-color: {C_SURFACE};
    color: {C_BORDER_HL};
    border: 1px solid {C_BORDER_DARK};
    padding: 4px 6px;
    font-weight: bold;
    font-size: 11px;
}}

/* Buttons (2px bevels) */
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

QPushButton#btnDanger {{
    background-color: {C_DANGER};
    color: #FFFFFF;
    border-top-color: #9C3B3B;
    border-left-color: #9C3B3B;
    border-bottom-color: #4A1010;
    border-right-color: #4A1010;
}}
QPushButton#btnDanger:hover {{
    background-color: #912727;
}}
QPushButton#btnDanger:pressed {{
    border-top-color: #4A1010;
    border-left-color: #4A1010;
    border-bottom-color: #9C3B3B;
    border-right-color: #9C3B3B;
}}

QPushButton#btnPrimary {{
    color: {C_BORDER_HL};
    border-top-color: #9C8542;
    border-left-color: #9C8542;
}}

/* Text Inputs */
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

/* CheckBoxes */
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

/* Splitter */
QSplitter::handle {{
    background-color: {C_BORDER_DARK};
    width: 4px;
}}

/* Scrollbars - ZERO WHITE ARTIFACTS */
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
QScrollBar::sub-line:vertical {{
    subcontrol-position: top;
}}
QScrollBar::add-line:vertical {{
    subcontrol-position: bottom;
}}
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


class PromptEditDialog(QDialog):
    """Direct modal editor for prompt text. Saves immediately on Ctrl+Enter."""
    def __init__(self, parent, item_id: str, seq: int, text: str):
        super().__init__(parent)
        self.item_id = item_id
        self.setWindowTitle(f"Edit Prompt [seq {seq}]")
        self.resize(520, 260)
        self.setStyleSheet(GOLDEN_DEFAULT_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        lbl = QLabel(f"Editing queued message {item_id}:")
        lbl.setStyleSheet(f"color: {C_BORDER_HL}; font-weight: bold;")
        layout.addWidget(lbl)

        self.txt = QTextEdit()
        self.txt.setPlainText(text)
        layout.addWidget(self.txt)

        btn_row = QHBoxLayout()
        btn_save = QPushButton("Save (Ctrl+Enter)")
        btn_save.setObjectName("btnPrimary")
        btn_save.clicked.connect(self.accept)
        btn_row.addWidget(btn_save)

        btn_cancel = QPushButton("Cancel (Esc)")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        layout.addLayout(btn_row)

        QShortcut(QKeySequence("Ctrl+Return"), self, self.accept)

    def get_text(self) -> str:
        return self.txt.toPlainText().strip()


class QueueManagerWindow(QMainWindow):
    def __init__(self, initial_session_id: str = None):
        super().__init__()
        self.setWindowTitle("SAIPATCH Native Queue Monitor")
        self.resize(920, 600)
        self.setMinimumSize(700, 460)
        self.setStyleSheet(GOLDEN_DEFAULT_QSS)

        self.db_path = queue_core.find_db_path()
        self.current_session_id = initial_session_id
        self.sessions = []
        self.current_queue = []
        self.current_history = []
        self.selected_item_id = None
        self._cached_queue_sig = None
        self._cached_history_sig = None

        self._build_ui()

        # Instant Delete shortcuts
        QShortcut(QKeySequence("Delete"), self, self._on_drop_clicked)
        QShortcut(QKeySequence("F5"), self, lambda: self._refresh_data(force=True))

        # Polling timer
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_data)
        self.timer.start(800)

        # Initial load
        self._refresh_data(force=True)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(4)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter, 1)

        # --- LEFT SIDEBAR (Sessions) ---
        sidebar_frame = QFrame()
        sidebar_frame.setObjectName("sidebarFrame")
        sidebar_layout = QVBoxLayout(sidebar_frame)
        sidebar_layout.setContentsMargins(4, 4, 4, 4)
        sidebar_layout.setSpacing(4)

        side_header = QHBoxLayout()
        lbl_sidebar_title = QLabel("SESSIONS")
        lbl_sidebar_title.setStyleSheet(f"color: {C_BORDER_HL}; font-weight: bold; font-size: 11px;")
        side_header.addWidget(lbl_sidebar_title)

        self.chk_active_only = QCheckBox("Active only")
        self.chk_active_only.setChecked(True)
        self.chk_active_only.toggled.connect(lambda: self._refresh_data(force=True))
        side_header.addWidget(self.chk_active_only)
        sidebar_layout.addLayout(side_header)

        self.session_list = QListWidget()
        self.session_list.currentRowChanged.connect(self._on_session_selected)
        sidebar_layout.addWidget(self.session_list, 1)

        # Sidebar footer button: Delete session from list/DB
        self.btn_del_session = QPushButton("✖ Remove Session")
        self.btn_del_session.setObjectName("btnDanger")
        self.btn_del_session.setToolTip("Instantly remove selected session from list/DB (No dialog)")
        self.btn_del_session.clicked.connect(self._on_delete_session_clicked)
        sidebar_layout.addWidget(self.btn_del_session)

        splitter.addWidget(sidebar_frame)
        splitter.setStretchFactor(0, 0)
        sidebar_frame.setMinimumWidth(230)

        # --- RIGHT MAIN AREA ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        # Top Bar
        top_bar = QFrame()
        top_bar.setObjectName("toolbarFrame")
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(6, 4, 6, 4)
        top_bar_layout.setSpacing(8)

        self.lbl_session_name = QLabel("All Sessions (Unified Queue)")
        self.lbl_session_name.setStyleSheet(f"color: {C_BORDER_HL}; font-weight: bold; font-size: 12px;")
        top_bar_layout.addWidget(self.lbl_session_name)

        self.lbl_session_dir = QLabel("")
        self.lbl_session_dir.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px;")
        top_bar_layout.addWidget(self.lbl_session_dir, 1)

        self.chk_pin = QCheckBox("Pin (Always on Top)")
        self.chk_pin.toggled.connect(self._toggle_pin)
        top_bar_layout.addWidget(self.chk_pin)

        btn_refresh = QPushButton("⟳")
        btn_refresh.setToolTip("Refresh Now (F5)")
        btn_refresh.setFixedWidth(32)
        btn_refresh.clicked.connect(lambda: self._refresh_data(force=True))
        top_bar_layout.addWidget(btn_refresh)

        right_layout.addWidget(top_bar)

        # Status / Active Turn Card
        self.status_card = QFrame()
        self.status_card.setObjectName("statusCard")
        status_card_layout = QVBoxLayout(self.status_card)
        status_card_layout.setContentsMargins(8, 6, 8, 6)
        status_card_layout.setSpacing(3)

        row_stat1 = QHBoxLayout()
        self.lbl_status_pill = QLabel("[IDLE]")
        self.lbl_status_pill.setStyleSheet(f"color: {C_ACCENT_TEAL}; font-weight: bold; font-size: 11px;")
        row_stat1.addWidget(self.lbl_status_pill)

        self.lbl_turn_step = QLabel("Turn in progress: None")
        self.lbl_turn_step.setStyleSheet(f"color: {C_TEXT_PRIMARY}; font-size: 11px;")
        row_stat1.addWidget(self.lbl_turn_step, 1)
        status_card_layout.addLayout(row_stat1)

        self.lbl_turn_prompt = QLabel("Prompt: -")
        self.lbl_turn_prompt.setStyleSheet(f"color: {C_TEXT_SECONDARY}; font-size: 11px;")
        status_card_layout.addWidget(self.lbl_turn_prompt)

        right_layout.addWidget(self.status_card)

        # Tabs for [Pending Queue] and [Sent History]
        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, 1)

        # TAB 1: Pending Queue
        tab_queue = QWidget()
        tab_q_layout = QVBoxLayout(tab_queue)
        tab_q_layout.setContentsMargins(2, 2, 2, 2)
        tab_q_layout.setSpacing(4)

        self.table_queue = QTableWidget()
        self.table_queue.setColumnCount(5)
        self.table_queue.setHorizontalHeaderLabels(["#", "Project", "Seq", "Prompt Text", "Time"])
        self.table_queue.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table_queue.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table_queue.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table_queue.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table_queue.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table_queue.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_queue.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_queue.verticalHeader().setVisible(False)
        self.table_queue.cellDoubleClicked.connect(self._on_edit_clicked)
        self.table_queue.itemSelectionChanged.connect(self._on_queue_selection_changed)
        tab_q_layout.addWidget(self.table_queue, 1)

        # Quick Add Input Bar
        add_bar = QHBoxLayout()
        add_bar.setSpacing(6)
        lbl_add = QLabel("+ Queue:")
        lbl_add.setStyleSheet(f"color: {C_BORDER_HL}; font-weight: bold;")
        add_bar.addWidget(lbl_add)

        self.input_prompt = QLineEdit()
        self.input_prompt.setPlaceholderText("Type prompt and press Enter to queue instantly...")
        self.input_prompt.returnPressed.connect(self._on_add_prompt)
        add_bar.addWidget(self.input_prompt, 1)

        btn_add = QPushButton("+ Queue Prompt")
        btn_add.setObjectName("btnPrimary")
        btn_add.clicked.connect(self._on_add_prompt)
        add_bar.addWidget(btn_add)

        tab_q_layout.addLayout(add_bar)

        # Action Buttons Bar
        action_bar = QFrame()
        action_bar.setObjectName("toolbarFrame")
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(6, 4, 6, 4)
        action_layout.setSpacing(8)

        self.btn_up = QPushButton("▲ Up")
        self.btn_up.clicked.connect(self._on_up_clicked)
        action_layout.addWidget(self.btn_up)

        self.btn_down = QPushButton("▼ Down")
        self.btn_down.clicked.connect(self._on_down_clicked)
        action_layout.addWidget(self.btn_down)

        self.btn_edit = QPushButton("✏ Edit")
        self.btn_edit.clicked.connect(self._on_edit_clicked)
        action_layout.addWidget(self.btn_edit)

        # Instant Drop (Zero confirmation popup)
        self.btn_drop = QPushButton("✖ Drop (Del)")
        self.btn_drop.setObjectName("btnDanger")
        self.btn_drop.setToolTip("Instant delete without confirmation")
        self.btn_drop.clicked.connect(self._on_drop_clicked)
        action_layout.addWidget(self.btn_drop)

        action_layout.addStretch(1)

        # Instant Clear (Zero confirmation popup)
        self.btn_clear = QPushButton("🗑 Clear Queue")
        self.btn_clear.setObjectName("btnDanger")
        self.btn_clear.setToolTip("Instantly empty pending queue without confirmation")
        self.btn_clear.clicked.connect(self._on_clear_clicked)
        action_layout.addWidget(self.btn_clear)

        tab_q_layout.addWidget(action_bar)
        self.tabs.addTab(tab_queue, "Pending Queue (0)")

        # TAB 2: Sent History
        tab_history = QWidget()
        tab_h_layout = QVBoxLayout(tab_history)
        tab_h_layout.setContentsMargins(2, 2, 2, 2)
        tab_h_layout.setSpacing(4)

        self.table_history = QTableWidget()
        self.table_history.setColumnCount(5)
        self.table_history.setHorizontalHeaderLabels(["#", "Project", "Promoted Seq", "Prompt Text", "Time Sent"])
        self.table_history.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table_history.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table_history.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table_history.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table_history.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table_history.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_history.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_history.verticalHeader().setVisible(False)
        tab_h_layout.addWidget(self.table_history, 1)

        self.tabs.addTab(tab_history, "Sent History (0)")

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(1, 1)

        # Bottom Status Bar
        status_bar = QHBoxLayout()
        status_bar.setContentsMargins(4, 2, 4, 2)
        self.lbl_db = QLabel(f"DB: {self.db_path}")
        self.lbl_db.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px;")
        status_bar.addWidget(self.lbl_db)

        status_bar.addStretch(1)

        self.lbl_feedback = QLabel("")
        self.lbl_feedback.setStyleSheet(f"color: {C_BORDER_HL}; font-size: 10px; font-weight: bold;")
        status_bar.addWidget(self.lbl_feedback)

        self.lbl_sync = QLabel("Sync: -")
        self.lbl_sync.setStyleSheet(f"color: {C_TEXT_MUTED}; font-size: 10px;")
        status_bar.addWidget(self.lbl_sync)

        main_layout.addLayout(status_bar)

    def _toggle_pin(self, checked: bool):
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _on_session_selected(self, row: int):
        if row == 0:
            self.current_session_id = None
        elif 0 < row <= len(self.sessions):
            self.current_session_id = self.sessions[row - 1]["id"]
        self._refresh_data(force=True)

    def _on_queue_selection_changed(self):
        sel_rows = self.table_queue.selectionModel().selectedRows()
        if sel_rows:
            r = sel_rows[0].row()
            if 0 <= r < len(self.current_queue):
                self.selected_item_id = self.current_queue[r]["id"]
                return
        self.selected_item_id = None

    @pyqtSlot()
    def _refresh_data(self, force: bool = False):
        now_str = datetime.now().strftime("%H:%M:%S")
        self.lbl_sync.setText(f"Sync: {now_str}")

        try:
            # 1. Update sessions list
            all_summaries = queue_core.get_sessions_summary(limit=35, db_path=self.db_path)

            if self.chk_active_only.isChecked():
                filtered_summaries = [s for s in all_summaries if s["is_process_live"] or s["pending_count"] > 0]
            else:
                filtered_summaries = all_summaries

            self.sessions = filtered_summaries
            total_pending = sum(s["pending_count"] for s in all_summaries)

            # Check if sidebar items need rebuilding
            sidebar_sig = tuple((s["id"], s["state"], s["pending_count"], s["is_process_live"]) for s in filtered_summaries)
            current_row = self.session_list.currentRow()

            if getattr(self, "_cached_sidebar_sig", None) != sidebar_sig or force:
                self._cached_sidebar_sig = sidebar_sig
                self.session_list.blockSignals(True)
                self.session_list.clear()

                # Row 0: All sessions
                badge = f" [{total_pending}]" if total_pending > 0 else ""
                it_all = QListWidgetItem(f"★ All Sessions (Unified){badge}")
                it_all.setData(Qt.ItemDataRole.UserRole, None)
                self.session_list.addItem(it_all)

                target_row = 0
                for i, s in enumerate(filtered_summaries):
                    p_badge = f" [{s['pending_count']}]" if s['pending_count'] > 0 else ""
                    if s["is_process_live"] and s["state"] == "RUNNING":
                        st_tag = "● RUNNING"
                    elif s["state"] == "FAILED":
                        st_tag = "✖ FAILED"
                    elif s["state"] == "NEEDS_HUMAN":
                        st_tag = "■ INPUT"
                    else:
                        st_tag = "○ IDLE"

                    txt = f"{s['project_name']}{p_badge}\n  {st_tag}"
                    it = QListWidgetItem(txt)
                    it.setData(Qt.ItemDataRole.UserRole, s["id"])
                    self.session_list.addItem(it)

                    if self.current_session_id and s["id"] == self.current_session_id:
                        target_row = i + 1

                self.session_list.setCurrentRow(target_row)
                self.session_list.blockSignals(False)

            # 2. Update Details & Status Card
            if self.current_session_id is None:
                self.lbl_session_name.setText("★ All Sessions (Unified Queue)")
                self.lbl_session_dir.setText(f"Total projects: {len(all_summaries)}")

                live_procs = [s for s in all_summaries if s["is_process_live"]]
                running_procs = [s for s in live_procs if s["state"] == "RUNNING"]
                failed_procs = [s for s in live_procs if s["state"] == "FAILED"]

                if running_procs:
                    names = ", ".join(s["project_name"] for s in running_procs)
                    self.lbl_status_pill.setText("[RUNNING]")
                    self.lbl_status_pill.setStyleSheet(f"color: {C_BORDER_HL}; font-weight: bold;")
                    self.lbl_turn_step.setText(f"Active in: {names}")
                elif failed_procs:
                    names = ", ".join(s["project_name"] for s in failed_procs)
                    self.lbl_status_pill.setText("[FAILED]")
                    self.lbl_status_pill.setStyleSheet(f"color: {C_DANGER_TEXT}; font-weight: bold;")
                    self.lbl_turn_step.setText(f"Failed turn in: {names}")
                elif live_procs:
                    names = ", ".join(s["project_name"] for s in live_procs)
                    self.lbl_status_pill.setText("[IDLE]")
                    self.lbl_status_pill.setStyleSheet(f"color: {C_ACCENT_TEAL}; font-weight: bold;")
                    self.lbl_turn_step.setText(f"OpenCode running in: {names} (Idle)")
                else:
                    self.lbl_status_pill.setText("[IDLE]")
                    self.lbl_status_pill.setStyleSheet(f"color: {C_ACCENT_TEAL}; font-weight: bold;")
                    self.lbl_turn_step.setText("No active OpenCode processes")

                self.lbl_turn_prompt.setText(f"Total pending across all sessions: {total_pending} item(s)")

                self.btn_up.setEnabled(False)
                self.btn_down.setEnabled(False)
                self.input_prompt.setEnabled(False)
                self.input_prompt.setPlaceholderText("Select a specific session from sidebar to queue prompts...")

                queue_items = queue_core.get_all_pending_queues(db_path=self.db_path)
                history_items = queue_core.get_all_sent_history(limit=50, db_path=self.db_path)
            else:
                cur_sess = next((s for s in all_summaries if s["id"] == self.current_session_id), None)
                if not cur_sess:
                    cur_sess = queue_core.get_active_session(db_path=self.db_path)
                    if cur_sess:
                        self.current_session_id = cur_sess["id"]

                proj_name = cur_sess["project_name"] if cur_sess else "Session"
                self.lbl_session_name.setText(f"Project: {proj_name}")
                self.lbl_session_dir.setText(f"Path: {cur_sess['directory'] if cur_sess else '-'}")

                st = queue_core.get_session_status(self.current_session_id, db_path=self.db_path)
                state = st["state"]
                detail = st.get("detail", "")
                is_live = st["is_process_live"]

                if state == "FAILED":
                    self.lbl_status_pill.setText("[FAILED]")
                    self.lbl_status_pill.setStyleSheet(f"color: {C_DANGER_TEXT}; font-weight: bold;")
                elif state == "RUNNING" and is_live:
                    self.lbl_status_pill.setText("[RUNNING]")
                    self.lbl_status_pill.setStyleSheet(f"color: {C_BORDER_HL}; font-weight: bold;")
                elif state == "NEEDS_HUMAN":
                    self.lbl_status_pill.setText("[INPUT REQUIRED]")
                    self.lbl_status_pill.setStyleSheet("color: #FF8C2E; font-weight: bold;")
                else:
                    self.lbl_status_pill.setText("[IDLE]")
                    self.lbl_status_pill.setStyleSheet(f"color: {C_ACCENT_TEAL}; font-weight: bold;")

                act = st.get("active_turn")
                if act:
                    self.lbl_turn_step.setText(f"Active Turn (promoted seq {act['promoted_seq']}): \"{act['text'][:65]}\"")
                    self.lbl_turn_prompt.setText(f"Status: {detail}")
                else:
                    self.lbl_turn_step.setText("Active Turn: None (Idle)")
                    self.lbl_turn_prompt.setText(f"Status: {detail}")

                self.btn_up.setEnabled(True)
                self.btn_down.setEnabled(True)
                self.input_prompt.setEnabled(True)
                self.input_prompt.setPlaceholderText(f"Queue prompt into {proj_name} and press Enter...")

                queue_items = queue_core.get_pending_queue(self.current_session_id, db_path=self.db_path)
                history_items = queue_core.get_sent_history(self.current_session_id, limit=50, db_path=self.db_path)

            # 3. Update Pending Queue Table ONLY IF CHANGED (PREVENTS SELECTION LOSS / COOLDOWN BUG!)
            queue_sig = tuple((item["id"], item["admitted_seq"], item["text"]) for item in queue_items)
            self.tabs.setTabText(0, f"Pending Queue ({len(queue_items)})")

            if self._cached_queue_sig != queue_sig or force:
                self._cached_queue_sig = queue_sig
                self.current_queue = queue_items

                prev_sel_id = self.selected_item_id
                self.table_queue.setRowCount(len(queue_items))
                restore_row = -1
                now_ms = time.time() * 1000

                for r, item in enumerate(queue_items):
                    p_name = item.get("project_name", "")
                    if not p_name and "session_id" in item:
                        s_obj = next((s for s in all_summaries if s["id"] == item["session_id"]), None)
                        p_name = s_obj["project_name"] if s_obj else item["session_id"][:8]

                    age_sec = max(0, int((now_ms - item["time_created"]) / 1000))
                    age_str = f"{age_sec}s ago" if age_sec < 60 else f"{age_sec // 60}m ago"

                    it_num = QTableWidgetItem(f"#{r+1}")
                    it_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                    it_proj = QTableWidgetItem(p_name)
                    it_proj.setForeground(QColor(C_BORDER_HL))

                    it_seq = QTableWidgetItem(str(item["admitted_seq"]))
                    it_seq.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    it_seq.setForeground(QColor(C_TEXT_SECONDARY))

                    snippet = item["text"].replace("\r\n", " ").replace("\n", " ")
                    it_text = QTableWidgetItem(snippet)

                    it_time = QTableWidgetItem(age_str)
                    it_time.setForeground(QColor(C_TEXT_MUTED))

                    self.table_queue.setItem(r, 0, it_num)
                    self.table_queue.setItem(r, 1, it_proj)
                    self.table_queue.setItem(r, 2, it_seq)
                    self.table_queue.setItem(r, 3, it_text)
                    self.table_queue.setItem(r, 4, it_time)

                    if prev_sel_id and item["id"] == prev_sel_id:
                        restore_row = r

                if restore_row >= 0:
                    self.table_queue.selectRow(restore_row)

            # 4. Update Sent History Table ONLY IF CHANGED
            history_sig = tuple((item["id"], item["promoted_seq"], item["text"]) for item in history_items)
            self.tabs.setTabText(1, f"Sent History ({len(history_items)})")

            if self._cached_history_sig != history_sig or force:
                self._cached_history_sig = history_sig
                self.current_history = history_items

                self.table_history.setRowCount(len(history_items))
                now_ms = time.time() * 1000

                for r, item in enumerate(history_items):
                    p_name = item.get("project_name", "")
                    if not p_name and "session_id" in item:
                        s_obj = next((s for s in all_summaries if s["id"] == item["session_id"]), None)
                        p_name = s_obj["project_name"] if s_obj else item["session_id"][:8]

                    age_sec = max(0, int((now_ms - item["time_created"]) / 1000))
                    age_str = f"{age_sec}s ago" if age_sec < 60 else f"{age_sec // 60}m ago"

                    it_num = QTableWidgetItem(f"#{r+1}")
                    it_num.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                    it_proj = QTableWidgetItem(p_name)
                    it_proj.setForeground(QColor(C_TEXT_SECONDARY))

                    it_seq = QTableWidgetItem(str(item["promoted_seq"]))
                    it_seq.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    it_seq.setForeground(QColor(C_BORDER_HL))

                    snippet = item["text"].replace("\r\n", " ").replace("\n", " ")
                    it_text = QTableWidgetItem(snippet)

                    it_time = QTableWidgetItem(age_str)
                    it_time.setForeground(QColor(C_TEXT_MUTED))

                    self.table_history.setItem(r, 0, it_num)
                    self.table_history.setItem(r, 1, it_proj)
                    self.table_history.setItem(r, 2, it_seq)
                    self.table_history.setItem(r, 3, it_text)
                    self.table_history.setItem(r, 4, it_time)

        except Exception as e:
            self.lbl_feedback.setText(f"Sync error: {e}")

    # --- Actions (Zero Dialogs / Instant Execution) ---

    def _on_up_clicked(self):
        if not self.selected_item_id or not self.current_session_id:
            return
        ok = queue_core.move_item_in_queue(self.current_session_id, self.selected_item_id, "up", db_path=self.db_path)
        if ok:
            self.lbl_feedback.setText("Moved up")
            self._refresh_data(force=True)

    def _on_down_clicked(self):
        if not self.selected_item_id or not self.current_session_id:
            return
        ok = queue_core.move_item_in_queue(self.current_session_id, self.selected_item_id, "down", db_path=self.db_path)
        if ok:
            self.lbl_feedback.setText("Moved down")
            self._refresh_data(force=True)

    def _on_edit_clicked(self):
        if not self.selected_item_id:
            return
        item = next((q for q in self.current_queue if q["id"] == self.selected_item_id), None)
        if not item:
            return

        dlg = PromptEditDialog(self, item["id"], item["admitted_seq"], item["text"])
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_text = dlg.get_text()
            if new_text:
                queue_core.edit_queued_prompt(item["id"], new_text, db_path=self.db_path)
                self.lbl_feedback.setText("Prompt updated")
                self._refresh_data(force=True)

    def _on_drop_clicked(self):
        """INSTANT DROP - NO confirmation box! No cooldown!"""
        sel_rows = self.table_queue.selectionModel().selectedRows()
        if not sel_rows:
            return
        row = sel_rows[0].row()
        if not (0 <= row < len(self.current_queue)):
            return

        item = self.current_queue[row]
        item_id = item["id"]

        ok = queue_core.delete_queued_item(item_id, db_path=self.db_path)
        if ok:
            self.lbl_feedback.setText(f"Dropped {item_id[:12]}...")
            # Remove row locally and update selection immediately
            self.table_queue.removeRow(row)
            self.current_queue.pop(row)
            self._cached_queue_sig = tuple((it["id"], it["admitted_seq"], it["text"]) for it in self.current_queue)
            self.tabs.setTabText(0, f"Pending Queue ({len(self.current_queue)})")
            
            # Select adjacent row so user can rapidly delete in succession
            next_row = min(row, len(self.current_queue) - 1)
            if next_row >= 0:
                self.table_queue.selectRow(next_row)
                self.selected_item_id = self.current_queue[next_row]["id"]
            else:
                self.selected_item_id = None

    def _on_clear_clicked(self):
        """INSTANT CLEAR - NO confirmation box!"""
        if self.current_session_id:
            count = queue_core.clear_queue(self.current_session_id, db_path=self.db_path)
            self.lbl_feedback.setText(f"Cleared {count} item(s)")
        else:
            count = queue_core.clear_all_queues(db_path=self.db_path)
            self.lbl_feedback.setText(f"Cleared all {count} item(s)")
        self.selected_item_id = None
        self._refresh_data(force=True)

    def _on_add_prompt(self):
        text = self.input_prompt.text().strip()
        if not text:
            return
        target_session = self.current_session_id
        if not target_session:
            if self.sessions:
                target_session = self.sessions[0]["id"]
            else:
                self.lbl_feedback.setText("No target session to queue into")
                return

        new_id = queue_core.add_queued_prompt(target_session, text, db_path=self.db_path)
        self.input_prompt.clear()
        self.selected_item_id = new_id
        self.lbl_feedback.setText(f"Queued {new_id[:12]}...")
        self._refresh_data(force=True)

    def _on_delete_session_clicked(self):
        """INSTANT DELETE SESSION - Removes historical dead session from DB."""
        if not self.current_session_id:
            self.lbl_feedback.setText("Cannot delete 'All Sessions' view")
            return

        target_id = self.current_session_id
        ok = queue_core.delete_session(target_id, db_path=self.db_path)
        if ok:
            self.lbl_feedback.setText(f"Removed session {target_id[:12]}")
            self.current_session_id = None
            self._refresh_data(force=True)


# ==========================================
# CLI Operations
# ==========================================
def run_cli():
    parser = argparse.ArgumentParser(description="SAIPATCH Queue Manager CLI / GUI")
    parser.add_argument("command", nargs="?", default="gui",
                        choices=["gui", "list", "status", "drop", "clear", "up", "down", "add"])
    parser.add_argument("arg1", nargs="?", help="Item ID / Index / Direction / Text")
    parser.add_argument("--session", "-s", help="Target Session ID")
    parser.add_argument("--db", help="Path to opencode.db")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()
    db_path = args.db or queue_core.find_db_path()

    if args.command == "gui":
        app = QApplication(sys.argv)
        
        # IRON LAW 1: Verdana, NON-ANTIALIASED everywhere, !important.
        font = QFont("Verdana", 9)
        font.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        app.setFont(font)
        
        win = QueueManagerWindow(initial_session_id=args.session)
        win.show()
        sys.exit(app.exec())

    session_id = args.session
    if not session_id:
        active = queue_core.get_active_session(db_path=db_path)
        if not active:
            print("Error: No active OpenCode session found.", file=sys.stderr)
            sys.exit(1)
        session_id = active["id"]

    if args.command == "status":
        st = queue_core.get_session_status(session_id, db_path=db_path)
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(f"Session: {session_id}")
            print(f"State  : {st['state']} ({st.get('detail', '')})")
            if st.get("active_turn"):
                t = st["active_turn"]
                print(f"Active : [seq {t['promoted_seq']}] \"{t['text']}\"")
            else:
                print("Active : None (Idle)")

    elif args.command == "list":
        queue = queue_core.get_pending_queue(session_id, db_path=db_path)
        if args.json:
            print(json.dumps(queue, indent=2))
        else:
            print(f"Pending Queue for session {session_id} ({len(queue)} items):")
            if not queue:
                print("  <Queue is empty>")
            for i, item in enumerate(queue):
                snippet = item["text"].replace("\n", " ")
                if len(snippet) > 60:
                    snippet = snippet[:57] + "..."
                print(f"  #{i+1:<2} [seq {item['admitted_seq']:<3}] {item['id']}: \"{snippet}\"")

    elif args.command == "drop":
        if not args.arg1:
            print("Error: Specify item ID or #index to drop", file=sys.stderr)
            sys.exit(1)
        target_id = args.arg1
        if target_id.isdigit():
            idx = int(target_id) - 1
            q = queue_core.get_pending_queue(session_id, db_path=db_path)
            if 0 <= idx < len(q):
                target_id = q[idx]["id"]
            else:
                print(f"Error: Index #{args.arg1} out of range (1-{len(q)})", file=sys.stderr)
                sys.exit(1)
        ok = queue_core.delete_queued_item(target_id, db_path=db_path)
        print(f"Dropped item {target_id}" if ok else "Item not found or already promoted")

    elif args.command == "clear":
        count = queue_core.clear_queue(session_id, db_path=db_path)
        print(f"Cleared {count} item(s) from session {session_id}")

    elif args.command == "up":
        target_id = args.arg1
        if target_id.isdigit():
            idx = int(target_id) - 1
            q = queue_core.get_pending_queue(session_id, db_path=db_path)
            if 0 <= idx < len(q):
                target_id = q[idx]["id"]
        ok = queue_core.move_item_in_queue(session_id, target_id, "up", db_path=db_path)
        print("Moved up" if ok else "Cannot move up")

    elif args.command == "down":
        target_id = args.arg1
        if target_id.isdigit():
            idx = int(target_id) - 1
            q = queue_core.get_pending_queue(session_id, db_path=db_path)
            if 0 <= idx < len(q):
                target_id = q[idx]["id"]
        ok = queue_core.move_item_in_queue(session_id, target_id, "down", db_path=db_path)
        print("Moved down" if ok else "Cannot move down")

    elif args.command == "add":
        if not args.arg1:
            print("Error: Specify prompt text to add", file=sys.stderr)
            sys.exit(1)
        new_id = queue_core.add_queued_prompt(session_id, args.arg1, db_path=db_path)
        print(f"Added queued prompt {new_id}")


if __name__ == "__main__":
    run_cli()
