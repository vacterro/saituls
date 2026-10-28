"""Real backend positive control for SAIPATCH generation 2.x (opt-in).

Starts the local queue fixture provider and a DISPOSABLE OpenCode server
(XDG/HOME redirected into the evidence tree), then drives cc1..cc5 through
live_native_queue.py against the native V2 scheduler. Every child process is
terminated in a finally block; nothing is left running.

Usage:
  python tests/saipatch_backend_smoke.py --exe <disposable opencode.exe> \
      --evidence <dir> [--timeout 240]

Exit 0 = strict native FIFO PASS (five successful terminal completions).
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"server never became ready: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, help="disposable opencode.exe path")
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()

    exe = Path(args.exe).resolve()
    if not exe.is_file():
        raise RuntimeError(f"disposable exe missing: {exe}")
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)

    provider_port = free_port()
    server_port = free_port()
    home = evidence / "home"
    config_dir = home / "xdgconfig"
    data_dir = home / "xdgdata"
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

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
    config_file = config_dir / "opencode" / "opencode.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")

    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "local"),
        "XDG_CONFIG_HOME": str(config_dir),
        "XDG_DATA_HOME": str(data_dir),
    })

    provider = server = None
    log_provider = evidence / "provider.log"
    log_server = evidence / "server.log"
    try:
        provider = subprocess.Popen(
            [sys.executable, str(HERE / "native_queue_fixture.py"), "--port", str(provider_port), "--delay", "8"],
            stdout=open(log_provider, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        wait_for(f"http://127.0.0.1:{provider_port}/v1/models", time.monotonic() + 30)
        print(f"provider ready on {provider_port}", flush=True)

        server = subprocess.Popen(
            [str(exe), "serve", "--port", str(server_port), "--hostname", "127.0.0.1"],
            env=env,
            stdout=open(log_server, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            cwd=str(evidence),
        )
        wait_for(f"http://127.0.0.1:{server_port}/config", time.monotonic() + 120)
        print(f"disposable server ready on {server_port}", flush=True)

        result = subprocess.run(
            [sys.executable, str(HERE / "live_native_queue.py"),
             "--url", f"http://127.0.0.1:{server_port}",
             "--evidence", str(evidence),
             "--model", "fixture/queue-fixture",
             "--timeout", str(args.timeout)],
            env=env, cwd=str(evidence), timeout=args.timeout + 90,
        )
        print(f"live_native_queue exit={result.returncode}", flush=True)
        return result.returncode
    finally:
        for process in (server, provider):
            if process is None:
                continue
            try:
                process.terminate()
                process.wait(timeout=15)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:
        print(f"FAIL backend smoke: {error}", file=sys.stderr)
        raise SystemExit(1)
