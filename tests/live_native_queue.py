"""Exercise the real native scheduler; opt-in, requires a running OpenCode server.

This is backend acceptance only. It cannot prove TUI Enter/key routing.
Uses a disposable session and directory; never replays admitted prompts.
"""
import argparse
import json
import time
import urllib.request
from pathlib import Path


def inspect_history(history, admissions):
    """Validate durable order; return completed IDs and the executing prompt.

    A provider step may end with tool-calls and start another step for the same
    prompt. Only finish=stop is the happy-path terminal oracle for this fixture.
    """
    expected = [a['id'] for a in admissions]
    promoted, complete = [], []
    current = running = None
    previous_seq = 0
    session_ids = {a['sessionID'] for a in admissions}
    for event in history['data']:
        seq = event['durable']['seq']
        if seq <= previous_seq:
            raise RuntimeError('history is not strictly ordered by durable sequence')
        previous_seq = seq
        data, kind = event['data'], event['type']
        if data.get('sessionID') not in session_ids:
            raise RuntimeError('history crosses session identity')
        if kind == 'session.next.prompted':
            if current is not None:
                raise RuntimeError('next prompt promoted before previous successful terminal step.ended')
            current = data['messageID']
            if len(promoted) >= len(expected) or current != expected[len(promoted)]:
                raise RuntimeError('duplicate, lost or out-of-order promotion')
            promoted.append(current)
        elif kind == 'session.next.step.started':
            if current is None or running is not None:
                raise RuntimeError('step started without prompt or with concurrent active step')
            running = data['assistantMessageID']
        elif kind == 'session.next.step.ended':
            if running is None or data['assistantMessageID'] != running:
                raise RuntimeError('terminal step does not match active assistant identity')
            running = None
            if data['finish'] == 'stop':
                complete.append(current)
                current = None
            elif data['finish'] != 'tool-calls':
                raise RuntimeError('non-success finish: ' + data['finish'])
        elif kind in ('session.next.step.failed', 'session.next.interrupted'):
            raise RuntimeError('failure/interrupt is not happy-path FIFO acceptance')
    return complete, current if running else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--model", help="Authorized provider/model override for this disposable session only")
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    work = (args.evidence / "work").resolve()
    work.mkdir(exist_ok=True)

    def request(path, body=None):
        req = urllib.request.Request(
            args.url.rstrip("/") + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else {"http_status": response.status}

    def save(name, data):
        (args.evidence / name).write_text(json.dumps(data, indent=2), encoding="utf-8")

    configured = args.model or request("/config").get("model")
    if not configured or "/" not in configured:
        raise RuntimeError("configure an explicit OpenCode model before live acceptance")
    provider, model = configured.split("/", 1)
    session = request("/api/session", {"location": {"directory": str(work)},
                                      "model": {"providerID": provider, "id": model}})["data"]
    save("live-session.json", session)
    prefix = "/api/session/" + session["id"]
    admitted = []
    start = time.monotonic()
    try:
        for number in range(1, 6):
            text = (
                f"cc{number}. This is a controlled SAIPATCH scheduler acceptance test. "
                "Run exactly one harmless shell command: python -c \"import time; time.sleep(5)\". "
                f"Then answer only cc{number} finished. Do not modify any files or run other commands."
            )
            admitted.append(request(prefix + "/prompt", {
                "prompt": {"text": text}, "delivery": "queue",
            })["data"])
            save("live-admissions.json", admitted)
            print(f"admitted cc{number} seq={admitted[-1]['admittedSeq']}", flush=True)
            if number == 1:
                # Promotion alone races the start of the native agent step.
                while time.monotonic() - start < args.timeout:
                    history = request(prefix + "/history")
                    save("live-history.json", history)
                    complete, executing = inspect_history(history, admitted)
                    if executing == admitted[0]['id']:
                        save('busy-barrier.json', history)
                        print('BUSY_BARRIER step.started cc1', flush=True)
                        break
                    if complete:
                        raise RuntimeError('cc1 finished before followers; fixture must keep it active longer')
                    time.sleep(0.5)
                else:
                    raise RuntimeError("first input never reached an active step.started")
        while time.monotonic() - start < args.timeout:
            history = request(prefix + "/history")
            save("live-history.json", history)
            kinds = [e.get("type") for e in history["data"]]
            print("history", len(kinds), sorted(set(str(k) for k in kinds)), flush=True)
            complete, executing = inspect_history(history, admitted)
            if len(complete) == 5 and executing is None:
                save("live-messages.json", request(prefix + "/message"))
                save('fifo-verdict.json', {'result': 'PASS', 'completed': complete,
                     'oracle': 'durable promotion -> step.started -> successful step.ended before next promotion',
                     'scope': 'backend only; real TUI gate required separately'})
                print('PASS: 5/5 successful terminal completions in strict native FIFO', flush=True)
                return 0
            time.sleep(3)
        raise RuntimeError("live gate timed out; inspect recorded native history")
    finally:
        result = request(prefix + "/interrupt", {})
        save("live-interrupt.json", result)


if __name__ == "__main__":
    raise SystemExit(main())
