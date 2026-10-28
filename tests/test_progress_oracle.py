"""Regression fixtures for the SAIPATCH TUI FIFO oracle (T-143 clause 6).

A. one active step at end of history        -> RUNNING / no fault
B. second step.started while one is active  -> OVERLAP
C. started A, ended A, started B            -> valid FIFO (no fault)
Plus ordering, ended-without-start and finish vocabulary coverage.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from saipatch_tui_driver import classify_history  # noqa: E402


def admitted(*ids):
    return [{"id": i} for i in ids]


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    return ok


def main() -> int:
    failures = 0

    # Fixture A: one active step at end of history is the valid RUNNING state.
    rows = [
        {"type": "session.next.prompted", "data": {"messageID": "u1"}},
        {"type": "session.next.step.started", "data": {"assistantMessageID": "a1"}},
    ]
    promoted, complete, state, fault = classify_history(rows, admitted("u1"))
    failures += 0 if check(
        "A. one active step -> RUNNING, no fault",
        promoted == ["u1"] and state == "RUNNING" and fault is None and complete == [],
        f"state={state} fault={fault}") else 1

    # Fixture B: a second start while a step is active is the only OVERLAP.
    rows = [
        {"type": "session.next.prompted", "data": {"messageID": "u1"}},
        {"type": "session.next.step.started", "data": {"assistantMessageID": "a1"}},
        {"type": "session.next.step.started", "data": {"assistantMessageID": "a2"}},
    ]
    promoted, complete, state, fault = classify_history(rows, admitted("u1"))
    failures += 0 if check(
        "B. second concurrent start -> OVERLAP",
        fault == "OVERLAP" and state == "OVERLAP",
        f"state={state} fault={fault}") else 1

    # Fixture C: sequential start/end pairs are valid FIFO.
    rows = [
        {"type": "session.next.prompted", "data": {"messageID": "u1"}},
        {"type": "session.next.step.started", "data": {"assistantMessageID": "a1"}},
        {"type": "session.next.step.ended", "data": {"assistantMessageID": "a1", "finish": "stop"}},
        {"type": "session.next.prompted", "data": {"messageID": "u2"}},
        {"type": "session.next.step.started", "data": {"assistantMessageID": "a2"}},
        {"type": "session.next.step.ended", "data": {"assistantMessageID": "a2", "finish": "stop"}},
    ]
    promoted, complete, state, fault = classify_history(rows, admitted("u1", "u2"))
    failures += 0 if check(
        "C. started/ended/start -> valid FIFO",
        promoted == ["u1", "u2"] and complete == ["u1", "u2"] and state == "IDLE" and fault is None,
        f"state={state} fault={fault}") else 1

    # Out-of-order promotion is ORDER, never OVERLAP.
    rows = [
        {"type": "session.next.prompted", "data": {"messageID": "u2"}},
    ]
    _, _, _, fault = classify_history(rows, admitted("u1", "u2"))
    failures += 0 if check("out-of-order promotion -> ORDER", fault == "ORDER", f"fault={fault}") else 1

    # End without start is a driver-visible anomaly, not OVERLAP.
    rows = [
        {"type": "session.next.step.ended", "data": {"assistantMessageID": "a1", "finish": "stop"}},
    ]
    _, _, _, fault = classify_history(rows, [])
    failures += 0 if check("ended-without-start -> ENDED_WITHOUT_START",
                           fault == "ENDED_WITHOUT_START", f"fault={fault}") else 1

    # finish=stop collects completion; finish=error names the vocabulary fault.
    rows = [
        {"type": "session.next.prompted", "data": {"messageID": "u1"}},
        {"type": "session.next.step.started", "data": {"assistantMessageID": "a1"}},
        {"type": "session.next.step.ended", "data": {"assistantMessageID": "a1", "finish": "error"}},
    ]
    _, complete, _, fault = classify_history(rows, admitted("u1"))
    failures += 0 if check("finish=error -> FINISH:error, nothing complete",
                           fault == "FINISH:error" and complete == [], f"fault={fault}") else 1

    # step.failed is a named fault and clears the active step.
    rows = [
        {"type": "session.next.prompted", "data": {"messageID": "u1"}},
        {"type": "session.next.step.started", "data": {"assistantMessageID": "a1"}},
        {"type": "session.next.step.failed", "data": {"assistantMessageID": "a1"}},
    ]
    _, _, state, fault = classify_history(rows, admitted("u1"))
    failures += 0 if check("step.failed -> STEP_FAILED, back to IDLE",
                           fault == "STEP_FAILED" and state == "IDLE", f"state={state} fault={fault}") else 1

    # tool-calls finishes do not complete a turn but are not faults.
    rows = [
        {"type": "session.next.prompted", "data": {"messageID": "u1"}},
        {"type": "session.next.step.started", "data": {"assistantMessageID": "a1"}},
        {"type": "session.next.step.ended", "data": {"assistantMessageID": "a1", "finish": "tool-calls"}},
        {"type": "session.next.step.started", "data": {"assistantMessageID": "a1"}},
        {"type": "session.next.step.ended", "data": {"assistantMessageID": "a1", "finish": "stop"}},
    ]
    _, complete, _, fault = classify_history(rows, admitted("u1"))
    failures += 0 if check("tool-calls then stop -> complete once, no fault",
                           complete == ["u1"] and fault is None, f"complete={complete} fault={fault}") else 1

    print(f"{'PASS (0 failures)' if failures == 0 else f'FAILURES: {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
