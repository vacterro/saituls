# Test Suites & Automation Probes

This directory contains test suites, verification harnesses, and automation probes for SAITULS.

## SAIPATCH & TUI Automation Probes

- `tests/saipatch_tui_driver.py`: Core TUI driver utility providing headless/terminal session driving, keystroke injection (`send_ctrl_v`), and stream history classification (`classify_history`). Used by `test_progress_oracle.py`, `test_saipatch_secret_hygiene.py`, and `saipatch_ctrlv_e2e.py`.
- `tests/native_paste_conpty.py`: Disposable real ConPTY bracketed-paste measurement probe (clipboard-independent) for terminal paste verification.
- `tests/saipatch_ctrlv_e2e.py`: End-to-end Ctrl+V injector integration harness verifying driver-to-receiver console signal arrival.
- `tests/saipatch_transcript_oracle.py`: Live transcript verification oracle for OpenCode native V2 transcript streaming.
