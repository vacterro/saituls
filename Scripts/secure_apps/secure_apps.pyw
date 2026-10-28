"""SAITULS Secure Apps launcher.

This file exists so Explorer, SECURE_APPS.ps1 and anything else that expects a
double-clickable ``.pyw`` entry point still find one. It owns NO security
logic, no widgets and no constants: the whole window lives in
``secure_apps_gui.py``, which is the single implementation and is imported
normally by the tests.

Two identical copies of a security surface are two places to fix a defect and
one place to forget, so this file is deliberately tiny -- if you are looking
for the Golden Default window, the setup state machine or the acceptance
runner wiring, they are in ``secure_apps_gui.py``.
"""
import ntpath
import sys

HERE = ntpath.dirname(ntpath.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import secure_apps_gui


if __name__ == "__main__":
    sys.exit(secure_apps_gui.main())
