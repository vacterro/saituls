# encoding: utf-8
"""Suite-wide guard for the headless Qt tests.

The Secure Apps GUI tests build a real ``SecureAppsWindow``, whose
``BrokerWorker`` lives on a ``QThread``. A worker that finishes after its
window has been torn down still delivers its queued ``operationDone`` on the
next turn of the event loop, and ``_apply_operation`` answers a failed
operation with a modal ``QMessageBox.warning``. pytest-qt pumps the event loop
between tests, so that modal is opened with no test running and nobody to
dismiss it: the whole run blocks inside ``exec()`` forever (measured on
``GuiTruthfulLockTests`` -> ``GuiLostKeyUserRoleTests``, 597/672 collected).

Every ``QMessageBox`` a test asserts on is patched, so no test wants a modal
on screen. One repeating timer closes any modal that does appear; a real
regression in that area still fails its own test, it just cannot deadlock the
suite.
"""

import os

# Must be decided before any QApplication exists; the GUI tests' own
# setdefault() calls would otherwise be too late for the first one.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

_GUARD = []


def _close_any_modal():
    from PyQt6.QtWidgets import QApplication

    widget = QApplication.activeModalWidget()
    if widget is not None:
        widget.close()


def _install_modal_guard():
    if _GUARD:
        return
    try:
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance() or QApplication([])
    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(_close_any_modal)
    timer.start()
    # The app keeps both alive; the list only marks the guard as installed.
    _GUARD.extend((app, timer))


@pytest.fixture(autouse=True, scope="session")
def _qt_modal_guard():
    _install_modal_guard()
    yield
