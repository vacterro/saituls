"""T-144 §29/§30 transcript oracle.

Reads a disposable TUI evidence directory and decides whether the EXISTING TUI
store rendered the native V2 transcript live. Uses only:

  - the host projector trace (plugin-debug.log `project-trace` lines): the exact
    synthetic events handed to the existing sync switch, i.e. the local store
    mutation trace;
  - the ConPTY visible fixture text captured by the driver (tui-verdict.json
    ambient[].stream_tail + tui-console.log).

Never uses GET /api/session/{id}/message (forbidden by §30).

Usage:
    python tests/saipatch_transcript_oracle.py <evidence-dir>
Exit 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TRACE = re.compile(
    r"project-trace (\S+) msg=(\S*) part=(\S*) sid=(\S*) role=(\S*) finish=(\S*) len=(\S*)"
)
ANSI = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1bP[^\x1b]*\x1b\\"
    r"|\x1b[()][A-Za-z0-9]"
)


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: saipatch_transcript_oracle.py <evidence-dir>")
        return 2
    ev = Path(sys.argv[1])
    debug = ev / "plugin-debug.log"
    verdict = ev / "tui-verdict.json"
    console = ev / "tui-console.log"
    for required in (debug, verdict):
        if not required.is_file():
            print(f"FAIL: missing {required.name}")
            return 1

    records = []
    for line in debug.read_text(encoding="utf-8", errors="replace").splitlines():
        match = TRACE.search(line)
        if match:
            event_type, msg, part, sid, role, finish, length = match.groups()
            records.append({
                "type": event_type,
                "msg": msg,
                "part": part,
                "sid": sid,
                "role": role,
                "finish": finish,
                "len": int(length) if length.isdigit() else -1,
            })
    if not records:
        print("FAIL: no project-trace records (host projector never ran)")
        return 1

    sessions = {r["sid"] for r in records if r["sid"]}
    if len(sessions) != 1:
        print(f"FAIL: expected one session in trace, saw {sorted(sessions)}")
        return 1

    assistant_ids = {r["msg"] for r in records if r["type"] == "message.part.delta" and r["msg"]}
    user_ids = {
        r["msg"] for r in records
        if r["role"] == "user" and r["msg"] and r["type"] == "message.updated"
    }
    delta_by_msg = {}
    for r in records:
        if r["type"] == "message.part.delta" and r["msg"]:
            delta_by_msg.setdefault(r["msg"], 0)
            if r["len"] > 0:
                delta_by_msg[r["msg"]] += r["len"]
    final_text_by_msg = {}
    for r in records:
        if r["type"] == "message.part.updated" and r["msg"] in assistant_ids and r["len"] > 0:
            final_text_by_msg[r["msg"]] = max(final_text_by_msg.get(r["msg"], 0), r["len"])

    data = json.loads(verdict.read_text(encoding="utf-8"))
    visible = " ".join(
        strip_ansi(str(entry.get("stream_tail", ""))) for entry in data.get("ambient", [])
    )
    if console.is_file():
        visible += " " + strip_ansi(console.read_text(encoding="utf-8", errors="replace"))
    markers = {f"cc{n}" for n in range(1, 6)}
    seen_markers = {m for m in markers if re.search(rf"\b{m}\b", visible)}
    # cc2 is the real-Ctrl+V paste payload, so its prompt marker is the pasted
    # text, not the literal token "cc2".
    paste = data.get("paste_payload") or {}
    cc2_visible = (paste.get("head") and paste.get("head") in visible) or "cc2" in seen_markers
    finished = "finished" in visible

    problems = []
    if len(user_ids) != 5:
        problems.append(f"user rows {len(user_ids)} (need 5 distinct ids)")
    if len(assistant_ids) != 5:
        problems.append(f"assistant rows {len(assistant_ids)} (need 5 distinct ids)")
    if set(assistant_ids) & user_ids:
        problems.append("assistant id reused as user id")
    for msg in assistant_ids:
        if delta_by_msg.get(msg, 0) <= 0:
            problems.append(f"assistant {msg} never grew (no text.delta)")
        if final_text_by_msg.get(msg, 0) <= 0:
            problems.append(f"assistant {msg} never got final text")
    required_markers = markers - {"cc2"}
    if seen_markers & required_markers != required_markers:
        problems.append(f"ConPTY markers missing {sorted(required_markers - seen_markers)}")
    if not cc2_visible:
        problems.append("ConPTY never showed the cc2 paste payload")
    if not finished:
        problems.append("ConPTY never showed fixture 'finished'")

    print("TRANSCRIPT ORACLE (§29/§30, local trace + ConPTY; no HTTP)")
    print(f"  session: {next(iter(sessions))}")
    print(f"  user messages: {len(user_ids)}")
    print(f"  assistant messages: {len(assistant_ids)}")
    print(f"  assistant with delta: {sum(1 for m in assistant_ids if delta_by_msg.get(m, 0) > 0)}")
    print(f"  assistant final text: {sum(1 for m in assistant_ids if final_text_by_msg.get(m, 0) > 0)}")
    print(f"  ConPTY markers: {sorted(seen_markers)}  finished={finished}")
    if problems:
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
