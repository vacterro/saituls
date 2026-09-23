"""E2E Ctrl+V injector control: real send_ctrl_v -> real console receiver.

Runs the receiver in a NEW console, attaches via the driver's send_ctrl_v,
then asserts the semantic 4-record sequence arrived. This is the driver-side
oracle (clause 9): if it fails the DRIVER is broken, not OpenCode.
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import saipatch_tui_driver as driver  # noqa: E402


def main() -> int:
    out = Path(tempfile.mkdtemp(prefix="saituls-ctrlv-")) / "ctrlv-e2e.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".json", ".ready"):
        p = out.with_suffix(suffix)
        if p.exists():
            p.unlink()
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "saipatch_ctrlv_receiver.py"), "--out", str(out)],
        creationflags=subprocess.CREATE_NEW_CONSOLE)
    try:
        ready = out.with_suffix(".ready")
        deadline = time.monotonic() + 30
        while not ready.exists():
            if proc.poll() is not None:
                print("receiver exited before ready", flush=True)
                return 2
            if time.monotonic() > deadline:
                print("receiver never ready", flush=True)
                return 2
            time.sleep(0.1)
        pid = json.loads(ready.read_text())["pid"]
        time.sleep(0.3)
        diag = driver.send_ctrl_v(pid)
        deadline = time.monotonic() + 20
        while not out.exists():
            if proc.poll() is not None and not out.exists():
                print("receiver exited without observation", flush=True)
                return 2
            if time.monotonic() > deadline:
                print("receiver never observed ctrl+v", flush=True)
                return 2
            time.sleep(0.1)
        result = json.loads(out.read_text(encoding="utf-8"))
        delivery = {"injector": diag, "observed": result}
        (out.parent / "ctrlv-e2e-full.json").write_text(json.dumps(delivery, indent=2), encoding="utf-8")
        print(json.dumps(delivery, indent=2))
        ok = diag.get("written") == 4 and result.get("semantic_ctrl_v") is True
        print("CTRLV_E2E_PASS" if ok else "CTRLV_E2E_FAIL")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
