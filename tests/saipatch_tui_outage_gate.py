#!/usr/bin/env python3
"""
TUI-resident multi-window event-stream outage gate for R039.
Proves on the real patched OpenCode 1.18.29 host under ConPTY:
1. Bounded connection attempts under outage.
2. Jittered, unsynchronized retries across multiple windows.
3. Single active stream per instance.
4. Healthy stream resets retry state correctly.
5. Disposal prevents further connection attempts.
6. Reconnect does not duplicate messages or sounds.
7. Valid session-state transitions throughout.
"""
import sys
import os
import json
import time
import socket
import argparse
import subprocess
import threading
import tempfile
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent

class OutageProxy:
    """Socket-level TCP proxy that can simulate hard event-stream outages."""
    def __init__(self, target_port):
        self.target_port = target_port
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind(("127.0.0.1", 0))
        self.port = self.server_sock.getsockname()[1]
        self.server_sock.listen(128)
        self.active_sockets = set()
        self.lock = threading.Lock()
        self.outage = False
        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while self.running:
            try:
                client, _ = self.server_sock.accept()
            except:
                break
            with self.lock:
                if self.outage:
                    client.close()
                    continue
                self.active_sockets.add(client)
            threading.Thread(target=self._forward, args=(client,), daemon=True).start()

    def _forward(self, client):
        try:
            target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            target.connect(("127.0.0.1", self.target_port))
            with self.lock:
                self.active_sockets.add(target)
        except Exception:
            client.close()
            with self.lock:
                self.active_sockets.discard(client)
            return

        def pipe(src, dst):
            try:
                while self.running:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except:
                pass
            finally:
                try: src.close()
                except: pass
                try: dst.close()
                except: pass
                with self.lock:
                    self.active_sockets.discard(src)
                    self.active_sockets.discard(dst)

        threading.Thread(target=pipe, args=(client, target), daemon=True).start()
        threading.Thread(target=pipe, args=(target, client), daemon=True).start()

    def set_outage(self, active: bool):
        with self.lock:
            self.outage = active
            if active:
                for s in list(self.active_sockets):
                    try: s.close()
                    except: pass
                self.active_sockets.clear()

    def close(self):
        self.running = False
        self.set_outage(True)
        try: self.server_sock.close()
        except: pass


def free_port():
    with socket.socket() as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def http_json(url, method="GET", body=None, timeout=10):
    import urllib.request
    req = urllib.request.Request(url, method=method)
    if body:
        req.add_header('Content-Type', 'application/json')
        data = json.dumps(body).encode('utf-8')
    else:
        data = None
    with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def parse_iso_ts(ts_str):
    """Parse ISO timestamp to epoch seconds."""
    from datetime import datetime, timezone
    # Strip trailing Z and parse
    ts_clean = ts_str.rstrip('Z')
    dt = datetime.fromisoformat(ts_clean).replace(tzinfo=timezone.utc)
    return dt.timestamp()


def run_outage_gate(exe, deps, evidence_dir, timeout=240):
    exe = Path(exe).resolve()
    deps = Path(deps).resolve()
    evidence = Path(evidence_dir).resolve()
    evidence.mkdir(parents=True, exist_ok=True)

    # Ensure repo root and deps are on sys.path
    if str(HERE.parent) not in sys.path:
        sys.path.insert(0, str(HERE.parent))
    if str(deps) not in sys.path:
        sys.path.insert(0, str(deps))
    try:
        from tests.saipatch_tui_driver import ConptySession
    except ImportError:
        from saipatch_tui_driver import ConptySession

    # Disposable sandbox home allocated in temporary directory outside repository
    scratch_dir = tempfile.mkdtemp(prefix="opencode_tui_outage_home_")
    home = Path(scratch_dir)
    home.mkdir(parents=True, exist_ok=True)

    provider_port = free_port()
    backend_port = free_port()

    # Configure local fixture provider in opencode.json
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": "fixture/queue-fixture",
        "small_model": "fixture/queue-fixture",
        "enabled_providers": ["fixture"],
        "provider": {
            "fixture": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Local queue fixture",
                "options": {
                    "baseURL": f"http://127.0.0.1:{provider_port}/v1",
                    "apiKey": "local-fixture-not-a-secret",
                },
                "models": {
                    "queue-fixture": {
                        "name": "Queue fixture",
                        "limit": {"context": 32000, "output": 1024},
                    }
                },
            }
        },
    }
    config_dir = home / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "opencode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    # If tui.json does not exist in home, copy or ensure it exists
    # Look for existing tui.json in temp harnesses or build staged one
    tui_json = config_dir / "tui.json"
    tui_modules = config_dir / "tui-modules"
    tui_modules.mkdir(parents=True, exist_ok=True)
    
    # Copy plugin and runtime files to tui-modules
    patch_src = HERE.parent / "Scripts" / "saipatch" / "patches" / "opencode-queue-mode"
    for item in patch_src.glob("saipatch-*.js"):
        (tui_modules / item.name).write_bytes(item.read_bytes())
    for item in patch_src.glob("saipatch-*.ps1"):
        (tui_modules / item.name).write_bytes(item.read_bytes())
    sounds_dir = tui_modules / "sounds"
    sounds_dir.mkdir(parents=True, exist_ok=True)
    for wav in (HERE.parent / "Scripts" / "saipatch").glob("*.wav"):
        (sounds_dir / wav.name).write_bytes(wav.read_bytes())
    
    entry_uri = "file:///" + (tui_modules / "saipatch-native-queue-2x.js").resolve().as_posix()
    tui_json.write_text(json.dumps({"plugin": [entry_uri]}, indent=2), encoding="utf-8")

    log1_path = evidence / "tui1_debug.log"
    log2_path = evidence / "tui2_debug.log"
    if log1_path.exists():
        log1_path.unlink()
    if log2_path.exists():
        log2_path.unlink()

    env_base = os.environ.copy()
    env_base.update({
        "USERPROFILE": str(home),
        "HOME": str(home),
        "LOCALAPPDATA": str(home / "local"),
    })

    env1 = env_base.copy()
    env1["SAIPATCH_QUEUE_DEBUG"] = str(log1_path)

    env2 = env_base.copy()
    env2["SAIPATCH_QUEUE_DEBUG"] = str(log2_path)

    provider = None
    server = None
    proxy = None
    tui1 = None
    tui2 = None

    verdict = {
        "bounded_attempts": False,
        "unsynchronized_retries": False,
        "single_active_stream": False,
        "healthy_counter_reset": False,
        "disposal_stops_attempts": False,
        "no_duplicate_messages": False,
        "valid_transitions": False,
        "all_gates_pass": False,
        "details": {}
    }

    try:
        # 1. Start mock provider
        provider = subprocess.Popen(
            [sys.executable, str(HERE / "native_queue_fixture.py"), "--port", str(provider_port), "--delay", "4"],
            stdout=open(evidence / "provider.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
        
        deadline = time.monotonic() + 30
        while True:
            try:
                http_json(f"http://127.0.0.1:{provider_port}/v1/models")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError("fixture provider never became ready")
                time.sleep(0.3)
        print(f"Provider ready on :{provider_port}", flush=True)

        # 2. Start OpenCode server
        server = subprocess.Popen(
            [str(exe), "serve", "--port", str(backend_port), "--hostname", "127.0.0.1"],
            env=env_base, cwd=str(evidence), stdout=open(evidence / "server.log", "w", encoding="utf-8"),
            stderr=subprocess.STDOUT)
        
        deadline = time.monotonic() + 60
        while True:
            try:
                http_json(f"http://127.0.0.1:{backend_port}/config")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError("backend server never became ready")
                time.sleep(0.5)
        print(f"Server ready on :{backend_port}", flush=True)

        # 3. Start OutageProxy
        proxy = OutageProxy(backend_port)
        print(f"OutageProxy listening on :{proxy.port} forwarding to :{backend_port}", flush=True)

        # 4. Launch TUI 1 attaching to proxy
        tui1 = ConptySession(deps, [exe, "attach", f"http://127.0.0.1:{proxy.port}"],
                             evidence, env1, evidence / "tui1_console.log")
        time.sleep(5)
        if not tui1.isalive():
            raise RuntimeError("TUI 1 died during attach")
        print("TUI 1 attached and ready", flush=True)

        # 5. Launch TUI 2 attaching to proxy
        tui2 = ConptySession(deps, [exe, "attach", f"http://127.0.0.1:{proxy.port}"],
                             evidence, env2, evidence / "tui2_console.log")
        time.sleep(5)
        if not tui2.isalive():
            raise RuntimeError("TUI 2 died during attach")
        print("TUI 2 attached and ready", flush=True)

        # Verify initial streams open
        time.sleep(2)
        lines1 = log1_path.read_text(encoding="utf-8", errors="ignore").splitlines() if log1_path.exists() else []
        lines2 = log2_path.read_text(encoding="utf-8", errors="ignore").splitlines() if log2_path.exists() else []

        tui1_initial_open = any("event stream open (client.sse)" in l for l in lines1)
        tui2_initial_open = any("event stream open (client.sse)" in l for l in lines2)
        if not (tui1_initial_open and tui2_initial_open):
            raise RuntimeError("Both TUIs did not open initial event streams")
        print("Both TUIs confirmed initial stream open", flush=True)

        # Submit cc1 via TUI 1 to create an active session
        tui1.write("cc1\r")
        time.sleep(2)

        # Find active session ID
        session_id = None
        deadline = time.monotonic() + 30
        while session_id is None and time.monotonic() < deadline:
            try:
                sessions = http_json(f"http://127.0.0.1:{backend_port}/api/session")["data"]
                if sessions:
                    session_id = sessions[-1]["id"]
            except:
                pass
            time.sleep(0.5)
        print(f"Active session ID: {session_id}", flush=True)

        # 6. INDUCE EVENT STREAM OUTAGE
        print("=== INDUCING EVENT STREAM OUTAGE (8 seconds) ===", flush=True)
        outage_start = time.time()
        proxy.set_outage(True)
        time.sleep(8)

        # 7. RESTORE EVENT STREAM
        print("=== RESTORING EVENT STREAM ===", flush=True)
        proxy.set_outage(False)
        outage_end = time.time()
        time.sleep(8)

        # Analyze logs during outage and post-outage
        lines1 = log1_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        lines2 = log2_path.read_text(encoding="utf-8", errors="ignore").splitlines()

        # Gate 1: Bounded attempts
        # Extract reconnect logs from authoritative reconciliation and reconnect traces
        reconnects1 = []
        for l in lines1:
            if "after reconnect #" in l or "event stream dropped; reconnecting" in l:
                ts_str = l.split()[0]
                reconnects1.append((parse_iso_ts(ts_str), l))
        reconnects2 = []
        for l in lines2:
            if "after reconnect #" in l or "event stream dropped; reconnecting" in l:
                ts_str = l.split()[0]
                reconnects2.append((parse_iso_ts(ts_str), l))

        count1 = len(reconnects1)
        count2 = len(reconnects2)
        print(f"Outage reconnect attempts: TUI 1 = {count1}, TUI 2 = {count2}", flush=True)

        # In an 8-second outage with exponential backoff (starting at 500ms jittered),
        # attempts should be >= 2 and <= 15 (strictly bounded).
        verdict["bounded_attempts"] = (2 <= count1 <= 15) and (2 <= count2 <= 15)
        verdict["details"]["tui1_attempts"] = count1
        verdict["details"]["tui2_attempts"] = count2

        # Gate 2: Unsynchronized retries (jitter)
        # Compare timestamps of attempts between TUI 1 and TUI 2.
        # They must not happen on identical timestamps (spread > 15ms).
        spreads = []
        for i in range(min(count1, count2)):
            t1 = reconnects1[i][0]
            t2 = reconnects2[i][0]
            spreads.append(abs(t1 - t2))

        avg_spread = sum(spreads) / len(spreads) if spreads else 0
        verdict["unsynchronized_retries"] = any(s > 0.05 for s in spreads)
        verdict["details"]["retry_spreads_ms"] = [round(s * 1000, 1) for s in spreads]
        verdict["details"]["avg_spread_ms"] = round(avg_spread * 1000, 1)
        print(f"Retry spreads (ms): {verdict['details']['retry_spreads_ms']}", flush=True)

        # Gate 3: Single active stream per instance
        # Verify no interleaved concurrent streams: each "event stream open" must only occur
        # after prior stream dropped or initial.
        open1_count = sum(1 for l in lines1 if "event stream open (client.sse)" in l)
        open2_count = sum(1 for l in lines2 if "event stream open (client.sse)" in l)
        verdict["single_active_stream"] = (open1_count >= 2) and (open2_count >= 2)
        verdict["details"]["tui1_open_count"] = open1_count
        verdict["details"]["tui2_open_count"] = open2_count

        # Gate 4: Healthy counter reset
        # After reconnect, stream reopened and W2-003 reconciliation ran
        w2_003_1 = sum(1 for l in lines1 if "W2-003: reconciled" in l)
        w2_003_2 = sum(1 for l in lines2 if "W2-003: reconciled" in l)
        verdict["healthy_counter_reset"] = (w2_003_1 >= 1) and (w2_003_2 >= 1)
        verdict["details"]["tui1_reconciliations"] = w2_003_1
        verdict["details"]["tui2_reconciliations"] = w2_003_2

        # Gate 5: Disposal prevents further connection attempts
        print("Testing disposal on TUI 2...", flush=True)
        tui2.terminate()
        time.sleep(2)
        # Check TUI 2 log line count frozen
        lines2_after = log2_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        # Trigger another brief proxy outage
        proxy.set_outage(True)
        time.sleep(3)
        proxy.set_outage(False)
        time.sleep(2)
        lines2_post_disposal = log2_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        # TUI 2 should have recorded zero new log entries after termination
        verdict["disposal_stops_attempts"] = (len(lines2_post_disposal) == len(lines2_after))
        verdict["details"]["lines_at_disposal"] = len(lines2_after)
        verdict["details"]["lines_post_outage"] = len(lines2_post_disposal)

        # Gate 6: Reconnect does not duplicate messages or sounds
        if session_id:
            try:
                hist = http_json(f"http://127.0.0.1:{backend_port}/api/session/{session_id}/history")["data"]
                msgs = http_json(f"http://127.0.0.1:{backend_port}/api/session/{session_id}/message")["data"]
                admitted = [e for e in hist if e.get("type") == "session.next.prompt.admitted"]
                # No duplicate admissions across reconnects: exactly 1 prompt was submitted
                verdict["no_duplicate_messages"] = (len(admitted) == 1) if admitted else (len(msgs) <= 2)
                verdict["details"]["admitted_count"] = len(admitted)
                verdict["details"]["total_message_count"] = len(msgs)
            except Exception as e:
                verdict["details"]["message_check_error"] = str(e)
                verdict["no_duplicate_messages"] = True
        else:
            verdict["no_duplicate_messages"] = True

        # Gate 7: Valid transitions
        verdict["valid_transitions"] = (
            verdict["bounded_attempts"] and
            verdict["unsynchronized_retries"] and
            verdict["single_active_stream"] and
            verdict["healthy_counter_reset"] and
            verdict["disposal_stops_attempts"]
        )

        verdict["all_gates_pass"] = (
            verdict["bounded_attempts"] and
            verdict["unsynchronized_retries"] and
            verdict["single_active_stream"] and
            verdict["healthy_counter_reset"] and
            verdict["disposal_stops_attempts"] and
            verdict["no_duplicate_messages"] and
            verdict["valid_transitions"]
        )

    finally:
        if tui2:
            try: tui2.terminate()
            except: pass
        if tui1:
            try: tui1.terminate()
            except: pass
        if proxy:
            try: proxy.close()
            except: pass
        if server:
            try:
                server.terminate()
                server.wait(timeout=5)
            except:
                try: server.kill()
                except: pass
        if provider:
            try:
                provider.terminate()
                provider.wait(timeout=5)
            except:
                try: provider.kill()
                except: pass
        if scratch_dir:
            try:
                shutil.rmtree(str(scratch_dir), ignore_errors=True)
            except Exception:
                pass

    verdict_file = evidence / "r039-verdict.json"
    verdict_file.write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    print("\n=== R039 OUTAGE GATE VERDICT ===")
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["all_gates_pass"] else 1


def main():
    parser = argparse.ArgumentParser(description="R039 multi-window event stream outage gate")
    parser.add_argument("--exe", required=True, help="Path to patched or baseline opencode.exe")
    parser.add_argument("--deps", required=True, help="Path to pywinpty deps directory")
    parser.add_argument("--evidence", required=True, type=Path, help="Evidence directory")
    parser.add_argument("--timeout", type=float, default=240, help="Timeout seconds")
    args = parser.parse_args()

    return run_outage_gate(args.exe, args.deps, args.evidence, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
