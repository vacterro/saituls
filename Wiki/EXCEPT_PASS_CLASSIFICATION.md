# Python Exception Suppression Classification (T-172)

## Overview
Audit and classification of exception handling (`except ...: pass` / `continue`) across first-party Python scripts in SAITULS.

## Classification Categories

### 1. Best-Effort Console & Environment Setup
- **Locations**: `Scripts/saipatch/clipboard+.pyw:72-77`, CLI wrappers
- **Pattern**: `sys.stdout.reconfigure(encoding="utf-8")` inside `try...except Exception: pass`.
- **Rationale**: Harmless feature detection on platforms or redirected stdio where `reconfigure` is unavailable or fails.

### 2. Speculative Format & Encoding Probes
- **Locations**: `Scripts/saipatch/clipboard+.pyw:518-589`, `Scripts/secure_apps/sa_privhelper.py`
- **Pattern**: Trying `unquote`, `b64decode`, or format decoding on arbitrary clipboard / URL tokens.
- **Rationale**: An unparseable token simply means it is not that specific format; ignoring parse failure and testing next candidate is by design.

### 3. Teardown and Cleanup in Finally Blocks
- **Locations**: `Scripts/saipatch/clipboard+.pyw:1263, 1958`, `Scripts/secure_apps/sa_privtask.py`
- **Pattern**: `CloseClipboard()`, `CloseHandle()`, or socket cleanup in `finally:` or termination handlers.
- **Rationale**: Defensive cleanup during shutdown when a resource might already have been closed or released.

### 4. Non-Fatal Daemon & IPC Probes
- **Locations**: `Scripts/saipatch/clipboard+.pyw`, `Scripts/secure_apps/sa_broker.py`, `Scripts/saipatch/queue_core.py`
- **Pattern**: Socket connect / named-pipe probe to detect whether background resident is running.
- **Rationale**: If daemon is not running, connection failure is normal and triggers alternate execution paths.

### 5. Repaired Silent Failures
- **Location**: `Scripts/saipatch/clipboard+.pyw:load_existing_hashes`
- **Fix**: Replaced silent swallow in file-hash loop with explicit warning and skip counter:
  ```python
  except Exception as exc:
      skipped += 1
      print("⚠️ Skipped unreadable file in the save folder: %s (%s)"
            % (os.path.basename(filepath), exc))
  ```
- **Validation**: Verified by `tests/test_clipboard_plus_control.py` (12/12 PASS).
