#!/usr/bin/env python3
"""LIMISAW Phase 2 probe — prove both Codex accounts return independent
quota snapshots via the app-server JSON-RPC protocol.

Read-only. Spawns `codex app-server --stdio` with a per-account CODEX_HOME
set ONLY in the child process environment (the global/user CODEX_HOME is
never touched). Performs the handshake (initialize -> wait -> initialized)
and calls account/rateLimits/read. Parses primary/secondary windows by
windowDurationMins. Never parses auth.json or prints tokens.

Usage:
    python limisaw_probe.py                 # probe both accounts from config
    python limisaw_probe.py --json          # emit machine-readable result
    python limisaw_probe.py --account 1     # probe only account 1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# --- constants -------------------------------------------------------------
HANDSHAKE_TIMEOUT_S = 15.0        # initialize + rateLimits call
RESPONSE_WINDOW_S = 12.0          # wait for each JSON-RPC response
CHILD_KILL_GRACE_S = 2.0

# Canonical window-duration -> label mapping (brief PHASE 2).
DURATION_LABELS = {
    300: "five_hour",
    10080: "weekly",
}


# --- config discovery ------------------------------------------------------
def home_dir() -> Path:
    """Resolve $HOME the way the PowerShell launchers do (Join-Path $HOME ...).

    On Windows $HOME is typically %USERPROFILE%. The launchers use the
    automatic $HOME which PowerShell resolves to the user profile.
    """
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    if not home:
        raise RuntimeError("could not resolve $HOME / USERPROFILE")
    return Path(home)


def account_homes_from_launchers() -> list[dict]:
    """Discover CODEX_HOME for account 1 + 2 by reading the two launcher
    scripts, exactly as the brief requires (do not guess from dir names).

    Account 1 launcher uses the DEFAULT Codex home (~\\.codex) and sets NO
    CODEX_HOME.  Account 2 launcher sets CODEX_HOME = ~\\.codex-account2.

    We parse the literal assignment in the .ps1 rather than executing it.
    """
    home = home_dir()
    primary = (home / ".codex").resolve()
    secondary = (home / ".codex-account2").resolve()

    # Launcher scripts are a machine-specific cross-check, not a runtime
    # dependency. Default to sibling dirs of the tool's own location; if they
    # are absent, the pure-CODEX_HOME defaults above are used unchanged.
    probe_dir = Path(__file__).resolve().parent
    launcher_root = probe_dir.parent / "_AI_STUFF_AGENTIC"
    accounts = [
        {"index": 1, "name": "main_codex", "codex_home": str(primary),
         "enabled": True,
         "launcher": str(launcher_root / "main_codex" / "Start-Codex-Main.ps1")},
        {"index": 2, "name": "main_codex2", "codex_home": str(secondary),
         "enabled": True,
         "launcher": str(launcher_root / "main_codex2" / "Start-Codex-Account2.ps1")},
    ]

    # Cross-check: if either launcher exists, read it and confirm the
    # CODEX_HOME it sets matches what we recorded. Never execute it.
    try:
        p1 = accounts[0]["launcher"]
        if Path(p1).exists():
            txt = Path(p1).read_text(encoding="utf-8", errors="replace")
            # Account 1 sets no CODEX_HOME -> default. Just confirm.
            if re.search(r"\$env:CODEX_HOME\s*=", txt):
                # Unexpected: launcher sets a custom home; parse it.
                m = re.search(r'\$env:CODEX_HOME\s*=\s*"([^"]+)"', txt)
                if m:
                    accounts[0]["codex_home"] = m.group(1)
    except Exception:
        pass
    try:
        p2 = accounts[1]["launcher"]
        if Path(p2).exists():
            txt = Path(p2).read_text(encoding="utf-8", errors="replace")
            m = re.search(r'\$env:CODEX_HOME\s*=\s*\$SecondaryCodexHome', txt)
            if m:
                # The launcher derives secondary from Join-Path $HOME ".codex-account2"
                m2 = re.search(r'SecondaryCodexHome\s*=\s*Join-Path\s+\$HOME\s+"([^"]+)"', txt)
                if m2:
                    accounts[1]["codex_home"] = str((home / m2.group(1)).resolve())
            else:
                m3 = re.search(r'\$env:CODEX_HOME\s*=\s*"([^"]+)"', txt)
                if m3:
                    accounts[1]["codex_home"] = m3.group(1)
    except Exception:
        pass
    return accounts


# --- JSON-RPC over stdio ---------------------------------------------------
class JsonRpcError(Exception):
    pass


class AppServerSession:
    """Minimal JSON-RPC 2.0 client over stdio for codex app-server.

    Each line on stdout is a JSON-RPC message (response or notification).
    We pair responses by id. Notifications are drained and ignored (except
    for protocol correctness).
    """

    def __init__(self, proc: subprocess.Popen, label: str):
        self.proc = proc
        self.label = label
        self._next_id = 1
        self._lock = threading.Lock()
        self._responses: dict[int, dict] = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._closed = False

    def _read_loop(self) -> None:
        if self.proc.stdout is None:
            return
        try:
            for line in iter(self.proc.stdout.readline, b""):
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                if isinstance(msg, dict) and "id" in msg:
                    with self._lock:
                        self._responses[msg["id"]] = msg
        except Exception:
            pass

    def call(self, method: str, params=None, timeout: float = RESPONSE_WINDOW_S) -> dict:
        with self._lock:
            rid = self._next_id
            self._next_id += 1
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        data = (json.dumps(req) + "\n").encode("utf-8")
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if rid in self._responses:
                    return self._responses.pop(rid)
            time.sleep(0.02)
        raise JsonRpcError(f"{self.label}: timed out waiting for response to {method}")

    def notify(self, method: str, params=None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        data = (json.dumps(msg) + "\n").encode("utf-8")
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=CHILD_KILL_GRACE_S)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _resolve_codex_cmd() -> str:
    """Find the codex launcher on Windows (prefers codex.cmd / codex.exe)."""
    if os.name != "nt":
        return "codex"
    for name in ("codex.cmd", "codex.exe", "codex.bat"):
        for base in os.environ.get("PATH", "").split(os.pathsep):
            if not base:
                continue
            cand = os.path.join(base, name)
            if os.path.isfile(cand):
                return cand
    return "codex.cmd"


def start_app_server(codex_home: str, label: str) -> AppServerSession:
    """Spawn `codex app-server --stdio` with CODEX_HOME set ONLY in the
    child environment. The parent environment is not modified.
    """
    env = os.environ.copy()
    env["CODEX_HOME"] = codex_home
    # Defensive: never let a stray API key leak between accounts. The
    # launchers explicitly strip these; we mirror that for parity.
    for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        env.pop(k, None)

    creationflags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW so no console flashes.
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    # `codex` ships as a node .cmd shim on Windows; resolve the real path.
    codex_cmd = _resolve_codex_cmd()

    proc = subprocess.Popen(
        [codex_cmd, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=creationflags,
        bufsize=-1,
        text=False,  # binary; reader decodes lines
    )
    return AppServerSession(proc, label)


def handshake_and_read_rates(session: AppServerSession) -> dict:
    """initialize -> wait -> initialized -> account/rateLimits/read.

    Returns the parsed rate-limits response. Raises JsonRpcError on failure.
    """
    # 1. initialize
    init_resp = session.call(
        "initialize",
        {
            "clientInfo": {"name": "limisaw", "version": "0.1.0"},
            "capabilities": None,
        },
        timeout=RESPONSE_WINDOW_S,
    )
    if "error" in init_resp:
        raise JsonRpcError(f"initialize error: {init_resp['error']}")
    # 2. initialized notification
    session.notify("initialized")
    # 3. account/rateLimits/read (params: undefined -> omit)
    rl_resp = session.call("account/rateLimits/read", timeout=RESPONSE_WINDOW_S)
    if "error" in rl_resp:
        raise JsonRpcError(f"rateLimits error: {rl_resp['error']}")
    return rl_resp.get("result") or {}


def parse_windows(rate_limits: dict) -> dict:
    """Map primary/secondary (and rateLimitsByLimitId) windows by
    windowDurationMins. Never assumes primary=5h/secondary=weekly ordering.

    Returns:
        {
          "five_hour": {"remaining_percent": int|None, "resets_at": iso|None,
                         "used_percent": float|None, "window_duration_mins": 300|None,
                         "available": bool},
          "weekly":    {...},
          "raw_primary_dur": int|None,
          "raw_secondary_dur": int|None,
        }
    Missing buckets -> available=False, displayed as "--".
    """
    out = {
        "five_hour": {"available": False, "remaining_percent": None,
                      "resets_at": None, "used_percent": None,
                      "window_duration_mins": None},
        "weekly": {"available": False, "remaining_percent": None,
                   "resets_at": None, "used_percent": None,
                   "window_duration_mins": None},
        "raw_primary_dur": None,
        "raw_secondary_dur": None,
        "plan_type": None,
    }

    # rateLimits is the backward-compatible single-bucket snapshot.
    snap = rate_limits.get("rateLimits") or {}
    out["plan_type"] = snap.get("planType")

    candidates: list[tuple[int | None, dict]] = []
    primary = snap.get("primary")
    secondary = snap.get("secondary")
    if isinstance(primary, dict):
        d = primary.get("windowDurationMins")
        out["raw_primary_dur"] = d
        candidates.append((d, primary))
    if isinstance(secondary, dict):
        d = secondary.get("windowDurationMins")
        out["raw_secondary_dur"] = d
        candidates.append((d, secondary))

    # Also consider rateLimitsByLimitId (multi-bucket view) in case the
    # backend returns windows there instead.
    by_id = rate_limits.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for _lid, sub in by_id.items():
            if not isinstance(sub, dict):
                continue
            for key in ("primary", "secondary"):
                w = sub.get(key)
                if isinstance(w, dict):
                    candidates.append((w.get("windowDurationMins"), w))

    for dur, w in candidates:
        label = DURATION_LABELS.get(dur) if dur is not None else None
        if label is None or label not in out:
            continue
        if out[label]["available"]:
            # first match wins; do not overwrite
            continue
        used = w.get("usedPercent")
        resets = w.get("resetsAt")
        rem = None
        if isinstance(used, (int, float)):
            rem = max(0, min(100, 100 - used))
        out[label] = {
            "available": True,
            "remaining_percent": rem,
            "resets_at": _iso_from_epoch(resets),
            "used_percent": used if isinstance(used, (int, float)) else None,
            "window_duration_mins": dur,
        }
    return out


def _iso_from_epoch(val) -> str | None:
    """resetsAt is a UNIX epoch (seconds) as a number, per schema. Render
    ISO 8601 for logging/tooltip. Returns None if missing/invalid."""
    if val is None:
        return None
    try:
        f = float(val)
    except Exception:
        return None
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(f).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


# --- per-account probe -----------------------------------------------------
def probe_account(account: dict, verbose: bool = True) -> dict:
    label = account["name"]
    codex_home = account["codex_home"]
    result = {
        "index": account["index"],
        "name": label,
        "codex_home": codex_home,
        "ok": False,
        "error": None,
        "five_hour": {"available": False, "remaining_percent": None,
                      "resets_at": None},
        "weekly": {"available": False, "remaining_percent": None,
                   "resets_at": None},
        "raw": None,
        "fetched_at": None,
    }

    if not account.get("enabled", True):
        result["error"] = "disabled in config"
        return result
    if not Path(codex_home).exists():
        result["error"] = f"CODEX_HOME does not exist: {codex_home}"
        if verbose:
            print(f"[{label}] {result['error']}", file=sys.stderr)
        return result

    session = None
    try:
        session = start_app_server(codex_home, label)
        # Overall hard timeout for the whole handshake+read.
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
        rl = {}
        try:
            rl = handshake_and_read_rates(session)
        except JsonRpcError as e:
            result["error"] = str(e)
            if verbose:
                print(f"[{label}] {e}", file=sys.stderr)
            return result
        if time.monotonic() > deadline:
            result["error"] = "hard timeout exceeded"
            return result

        parsed = parse_windows(rl)
        result["ok"] = True
        result["five_hour"] = parsed["five_hour"]
        result["weekly"] = parsed["weekly"]
        result["raw"] = {
            "primary_dur": parsed["raw_primary_dur"],
            "secondary_dur": parsed["raw_secondary_dur"],
            "plan_type": parsed["plan_type"],
        }
        result["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if verbose:
            _print_human(result)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        if verbose:
            print(f"[{label}] {result['error']}", file=sys.stderr)
    finally:
        if session is not None:
            session.close()
    return result


def _print_human(r: dict) -> None:
    fh = r["five_hour"]
    wk = r["weekly"]
    def fmt(b):
        if not b["available"]:
            return "--"
        rem = b["remaining_percent"]
        rem_s = "--" if rem is None else f"{int(round(rem))}"
        return rem_s
    print(f"[{r['name']}] 5h={fmt(fh)} weekly={fmt(wk)}  home={r['codex_home']}")
    if fh["available"]:
        print(f"         5h  used={fh['used_percent']}  reset={fh['resets_at']}  dur={fh['window_duration_mins']}")
    else:
        print("         5h  unavailable")
    if wk["available"]:
        print(f"         wk  used={wk['used_percent']}  reset={wk['resets_at']}  dur={wk['window_duration_mins']}")
    else:
        print("         wk  unavailable")
    if r.get("error"):
        print(f"         ERROR: {r['error']}")


# --- CLI -------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="LIMISAW rate-limit probe")
    ap.add_argument("--account", type=int, choices=(1, 2), help="probe only this account")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--write-cache", metavar="PATH", help="write JSON snapshot to PATH (for the AHK UI to read)")
    args = ap.parse_args()

    accounts = account_homes_from_launchers()
    if args.account:
        accounts = [a for a in accounts if a["index"] == args.account]

    results = []
    for a in accounts:
        if not args.json and not args.write_cache:
            print(f"--- probing {a['name']} (CODEX_HOME={a['codex_home']}) ---")
        r = probe_account(a, verbose=not args.json and not args.write_cache)
        results.append(r)

    payload = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "accounts": results,
    }
    if args.write_cache:
        # Atomic write: temp then replace, so the UI never reads a half file.
        p = Path(args.write_cache)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(p)
        # Still print a one-line summary for any log capture.
        if not args.json:
            ok = sum(1 for r in results if r["ok"])
            print(f"cache written: {ok}/{len(results)} ok -> {args.write_cache}")
    elif args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        ok = sum(1 for r in results if r["ok"])
        print(f"\n=== {ok}/{len(results)} accounts returned a snapshot ===")
    # exit 0 if at least one account succeeded; non-zero only if all failed
    return 0 if any(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
