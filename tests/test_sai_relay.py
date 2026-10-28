"""Live-log-relay state machine checks (test_sai_relay.py).

Unit checks against poll_once(), the lease/state codecs and the argument
parsers. They run no account, touch no ACL, and make no claim about real
Windows tokens; token-level acceptance (the relay process actually publishing
into a staging dir the managed account can read) is a separate live check.

Run: python tests/test_sai_relay.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RELAY_MODULE = (os.environ.get("SAI_RELAY_MODULE")
                or os.path.join(REPO, "Scripts", "accounts", "sai_relay.py"))
relay = load_module("sai_relay", RELAY_MODULE)

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
    print("%s  %s%s" % ("PASS" if condition else "FAIL", name,
                        "" if condition else "  " + detail))


def write_text(path, text):
    with open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))


def append_text(path, text):
    with open(path, "ab") as handle:
        handle.write(text.encode("utf-8"))


def read_bytes(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def read_text(path):
    return read_bytes(path).decode("utf-8", "replace")


def make_state(source, staged, **over):
    lease = {
        "lease_id": "test-lease",
        "account": "ag2",
        "created_at": relay.now_iso(),
        "expires_at": time.time() + 3600,
    }
    options = {"interval": 0.02, "max_bytes": 1024, "keep": 2, "missing_grace_seconds": 300}
    state = relay.relay_state(lease, source, staged, options)
    state.update(over)
    return state


def gap_codes(state):
    return [g["code"] for g in state["gaps"]]


ACCOUNTS_STUB = r'''
import json, os, sys

args = sys.argv[1:]
verb = args[0] if args else ""
log = os.environ.get("FAKE_VERB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\n")
selector = next((a for a in args[1:] if not a.startswith("--")), "")
workdir = ""
for index, item in enumerate(args):
    if item == "--workdir" and index + 1 < len(args):
        workdir = args[index + 1]
if not selector or selector.startswith("nope"):
    print(json.dumps({"status": "ACCOUNT_NOT_FOUND", "selector": selector}))
elif verb == "status":
    print(json.dumps({"status": "ok",
                      "account_id": "antigravity:windows-user:S-1-5-21-4242",
                      "compact_label": "AG2",
                      "target_sid": "S-1-5-21-4242"}))
elif verb == "handoff-stage":
    staged = os.path.join(workdir or os.getcwd(), ".sai-handoff", "AG2")
    os.makedirs(staged, exist_ok=True)
    print(json.dumps({"status": "ok", "staging_dir": staged}))
else:
    print(json.dumps({"status": "UNKNOWN_VERB", "verb": verb}))
'''


def install_accounts_stub(tmp):
    """Speak to a stub registry, never to the operator's live one.

    Without this the suite resolves 'ag2' through whatever accounts root the
    machine happens to have, so it passes on one desk and fails on a clean
    runner. The stub answers only the two verbs the relay shells out for.
    """
    stub = os.path.join(tmp, "fake_accounts.py")
    with open(stub, "w", encoding="utf-8") as handle:
        handle.write(ACCOUNTS_STUB)
    wrapper = os.path.join(tmp, "fake_accounts.cmd")
    with open(wrapper, "w", encoding="utf-8") as handle:
        handle.write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, stub))
    os.environ["SAI_ACCOUNTS_EXE"] = wrapper
    os.environ["SAI_HANDOFF_ROOT"] = os.path.join(tmp, "handoff_root")
    os.makedirs(os.environ["SAI_HANDOFF_ROOT"], exist_ok=True)


def main():
    tmp = tempfile.mkdtemp(prefix="sai_relay_")
    install_accounts_stub(tmp)
    src = os.path.join(tmp, "app.log")
    dst = os.path.join(tmp, "staged.log")

    # --- first read delivers the whole existing content, once
    write_text(src, "one\n")
    state = make_state(src, dst)
    relay.poll_once(state)
    relay.poll_once(state)
    check("relay-delivers-existing-content", read_text(dst) == "one\n", repr(read_text(dst)))
    check("relay-first-cycle-no-gap", gap_codes(state) == [], repr(gap_codes(state)))

    # --- incremental append, no re-read of what was already delivered
    append_text(src, "two\n")
    relay.poll_once(state)
    check("relay-delivers-append", read_text(dst) == "one\ntwo\n", repr(read_text(dst)))
    check("relay-append-advances-offset", state["offset"] == 8, repr(state["offset"]))

    # --- a quiet source produces no writes at all
    size_before = os.path.getsize(dst)
    mtime_before = os.path.getmtime(dst)
    relay.poll_once(state)
    check("relay-quiet-source-no-write",
          os.path.getsize(dst) == size_before and os.path.getmtime(dst) == mtime_before)

    # --- in-place truncation is named, not silently resumed
    with open(src, "wb") as handle:
        handle.write(b"x\n")
    relay.poll_once(state)
    out = read_text(dst)
    check("relay-truncation-named", "SOURCE_TRUNCATED" in out, out[-160:])
    check("relay-truncation-restarts-at-zero", state["offset"] == 2, repr(state["offset"]))
    check("relay-truncation-counted", state["truncation_count"] == 1, repr(state["truncation_count"]))

    # --- replacement under the same name is a new object, so the stream restarts
    os.remove(src)
    time.sleep(0.05)
    write_text(src, "replaced\n")
    relay.poll_once(state)
    out = read_text(dst)
    check("relay-replacement-named", "SOURCE_REPLACED" in out, out[-160:])
    check("relay-replacement-restarts-at-zero", state["offset"] == 9, repr(state["offset"]))

    # --- missing source: a marker, then recovery is named too
    os.remove(src)
    relay.poll_once(state)
    first = read_text(dst)
    check("relay-missing-named", "SOURCE_MISSING" in first, first[-160:])
    check("relay-missing-freezes-offset", state["source_state"] == "SOURCE_MISSING",
          repr(state["source_state"]))
    write_text(src, "back\n")
    relay.poll_once(state)
    out = read_text(dst)
    check("relay-recovery-named", "SOURCE_RETURNED" in out, out[-160:])
    check("relay-recovery-delivers-new-bytes", read_text(dst).endswith("back\n"),
          repr(read_text(dst)[-40:]))

    # --- an absence past the grace interval is reported as lost, not stopped
    state["missing_grace_seconds"] = 0
    os.remove(src)
    relay.poll_once(state)
    relay.poll_once(state)
    check("relay-loss-named-after-grace", "SOURCE_LOST" in read_text(dst), read_text(dst)[-160:])

    # --- a partial trailing line is buffered until its newline arrives
    write_text(src, "head\n")
    relay.poll_once(state)
    settled = read_bytes(dst)
    append_text(src, "tail-without-newline")
    relay.poll_once(state)
    check("relay-partial-line-buffered", read_bytes(dst) == settled,
          repr(read_bytes(dst)[-60:]))
    # The source then goes quiet: the incomplete line must still reach the agent.
    state["partial_flush_seconds"] = 0.5
    time.sleep(0.6)
    relay.poll_once(state)
    out = read_text(dst)
    check("relay-partial-line-flushed-when-idle", "PARTIAL_LINE_FLUSHED" in out, out[-200:])
    check("relay-partial-line-bytes-counted", state["lost_bytes"] == 20, repr(state["lost_bytes"]))
    check("relay-partial-line-delivered", read_text(dst).endswith("tail-without-newline"),
          repr(read_text(dst)[-40:]))

    # --- a reparse point or a directory is refused, never followed
    link = os.path.join(tmp, "link.log")
    try:
        os.symlink(src, link)
    except OSError:
        link = None  # symlink creation needs an unprivileged dev-mode / admin right
    dirsrc = os.path.join(tmp, "adir")
    os.makedirs(dirsrc, exist_ok=True)
    for target in (link, dirsrc):
        if target is None:
            continue
        state_r = make_state(target, dst)
        relay.poll_once(state_r)
        check("relay-refuses-%s" % os.path.basename(target),
              state_r["source_state"] == "SOURCE_UNSAFE_OBJECT"
              and "SOURCE_UNSAFE_OBJECT" in gap_codes(state_r),
              "%s %s" % (state_r["source_state"], gap_codes(state_r)))

    # --- bounded output rotation keeps the primary path stable
    dst2 = os.path.join(tmp, "rot", "rot.log")
    os.makedirs(os.path.dirname(dst2), exist_ok=True)
    write_text(src, "a" * 6000 + "\n")
    state_rot = make_state(src, dst2, max_bytes=4096, keep=2)
    relay.poll_once(state_rot)
    relay.poll_once(state_rot)
    append_text(src, "b" * 6000 + "\n")
    relay.poll_once(state_rot)
    check("relay-rotates-output", os.path.exists(dst2 + ".1"), "no rotated generation appeared")
    check("relay-rotation-keeps-primary", os.path.exists(dst2), "primary path disappeared")
    check("relay-rotation-counted", state_rot["output_rotations"] >= 1, repr(state_rot["output_rotations"]))

    # --- the generation shift itself: exactly <keep> generations survive.
    # Four rotations with keep=2 must leave the primary plus exactly .1 and
    # .2, newest first. rotate_output is driven directly so the retained set
    # is the observable instead of a side effect of poll_once timing.
    rot_dir = os.path.join(tmp, "gen")
    os.makedirs(rot_dir, exist_ok=True)
    gen_path = os.path.join(rot_dir, "VRV.log")
    write_text(gen_path + ".0", "stray\n")      # outside 1..keep, so rotation drops it
    for generation in range(4):
        write_text(gen_path, ("generation%d\n" % generation) * 512)      # 6144 bytes
        moved = relay.rotate_output(gen_path, 4096, 2)
        check("relay-rotation-moves-%d" % generation, moved is True, repr(moved))
    append_text(gen_path, "live\n")             # what the caller appends after rotating
    names = sorted(os.listdir(rot_dir))
    check("relay-rotation-retains-exactly-keep",
          names == ["VRV.log", "VRV.log.1", "VRV.log.2"], repr(names))
    check("relay-rotation-newest-generation-first",
          read_text(gen_path + ".1").startswith("generation3")
          and read_text(gen_path + ".2").startswith("generation2"),
          repr((read_text(gen_path + ".1")[:12], read_text(gen_path + ".2")[:12])))

    # keep=0 keeps no history but must still bound the primary.
    zero_path = os.path.join(rot_dir, "zero.log")
    write_text(zero_path, "z" * 6000)
    zero_moved = relay.rotate_output(zero_path, 4096, 0)
    check("relay-rotation-zero-keep-bounds-primary",
          zero_moved is True and os.path.getsize(zero_path) == 0,
          "moved=%r size=%r" % (zero_moved, os.path.getsize(zero_path)))

    # A generation deeper than the configured keep is stale and must go.
    write_text(gen_path + ".7", "stale")
    write_text(gen_path, "n" * 6000)
    relay.rotate_output(gen_path, 4096, 2)
    check("relay-rotation-drops-stale-generation",
          not os.path.exists(gen_path + ".7"), repr(sorted(os.listdir(rot_dir))))

    # --- A2: a rotation that did not happen is never reported as a rotation
    # The old rotate_output set its "moved" flag when ANY generation moved,
    # so a history shift alone was reported as a rotation: the relay counted
    # a rotation that never happened, kept an oversized primary, and a
    # generation that could not move aside was overwritten -- its only copy
    # gone. Every refusal below runs on a disposable fixture.
    real_os = relay.os

    class FakeOs(object):
        """relay.os with named refusals. Everything else is the real os."""
        def __init__(self, real):
            self._real = real
            self.deny_replace = []
            self.deny_listdir = False
            self.deny_remove = False

        def __getattr__(self, name):
            return getattr(self._real, name)

        def replace(self, src, dst):
            for rule in self.deny_replace:
                if rule(src, dst):
                    raise PermissionError(13, "locked", str(dst))
            return self._real.replace(src, dst)

        def listdir(self, path):
            if self.deny_listdir:
                raise PermissionError(13, "denied", str(path))
            return self._real.listdir(path)

        def remove(self, path):
            if self.deny_remove:
                raise PermissionError(13, "locked", str(path))
            return self._real.remove(path)

    def rotate(path, max_bytes, keep, state=None):
        """Call rotate_output with whatever signature it has."""
        try:
            return relay.rotate_output(path, max_bytes, keep, state)
        except TypeError:
            return relay.rotate_output(path, max_bytes, keep)

    def call_out(call, *args):
        """(result, refusal-code). A raw OSError is named by its type: the
        old code let it escape, the new code names it."""
        try:
            return call(*args), None
        except relay.RelayError as ex:
            return None, ex.code
        except Exception as ex:
            return None, type(ex).__name__

    def under(fake, call, *args):
        relay.os = fake
        try:
            return call_out(call, *args)
        finally:
            relay.os = real_os

    # 1. the primary rename is denied: nothing was rotated and the refusal is named
    denied_dir = os.path.join(tmp, "rot_denied")
    os.makedirs(denied_dir, exist_ok=True)
    denied_path = os.path.join(denied_dir, "relay.log")
    write_text(denied_path + ".1", "history\n")
    write_text(denied_path, "p" * 6000)
    fake = FakeOs(real_os)
    fake.deny_replace = [lambda src, dst: os.path.basename(src) == "relay.log"]
    moved, code = under(fake, rotate, denied_path, 4096, 2)
    check("relay-rotation-primary-denied-not-reported",
          moved is None and code == "ROTATION_PRIMARY_DENIED",
          "moved=%r code=%r" % (moved, code))
    check("relay-rotation-primary-denied-keeps-primary",
          os.path.exists(denied_path) and os.path.getsize(denied_path) == 6000,
          repr(sorted(os.listdir(denied_dir))))
    check("relay-rotation-primary-denied-loses-nothing",
          os.path.getsize(denied_path) + len(read_bytes(denied_path + ".1"))
          + len(read_bytes(denied_path + ".2")) == 6000 + 8,
          repr(sorted(os.listdir(denied_dir))))

    # 2. the history rename is denied: the generation is NOT overwritten
    hist_dir = os.path.join(tmp, "rot_hist")
    os.makedirs(hist_dir, exist_ok=True)
    hist_path = os.path.join(hist_dir, "relay.log")
    write_text(hist_path + ".1", "history\n")
    write_text(hist_path, "h" * 6000)
    fake = FakeOs(real_os)
    fake.deny_replace = [lambda src, dst: os.path.basename(src).endswith(".1")]
    moved, code = under(fake, rotate, hist_path, 4096, 2)
    check("relay-rotation-history-denied-not-reported",
          moved is None and code == "ROTATION_HISTORY_DENIED",
          "moved=%r code=%r" % (moved, code))
    check("relay-rotation-history-denied-keeps-generation",
          read_text(hist_path + ".1") == "history\n"
          and os.path.getsize(hist_path) == 6000,
          repr(sorted(os.listdir(hist_dir))))

    # 3. the destination generation is locked: same rule, same naming
    lock_dir = os.path.join(tmp, "rot_lock")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, "relay.log")
    write_text(lock_path + ".1", "history\n")
    write_text(lock_path, "l" * 6000)
    fake = FakeOs(real_os)
    fake.deny_replace = [lambda src, dst: os.path.basename(dst) == "relay.log.2"]
    moved, code = under(fake, rotate, lock_path, 4096, 2)
    check("relay-rotation-locked-destination-not-reported",
          moved is None and code == "ROTATION_HISTORY_DENIED",
          "moved=%r code=%r" % (moved, code))
    check("relay-rotation-locked-destination-keeps-bytes",
          read_text(lock_path + ".1") == "history\n"
          and os.path.getsize(lock_path) == 6000,
          repr(sorted(os.listdir(lock_dir))))

    # 4. the directory listing is denied: the rotation still happened and the
    #    skipped sweep is said out loud
    list_dir = os.path.join(tmp, "rot_list")
    os.makedirs(list_dir, exist_ok=True)
    list_path = os.path.join(list_dir, "relay.log")
    write_text(list_path, "x" * 6000)
    state_list = make_state(src, list_path, max_bytes=4096, keep=2)
    fake = FakeOs(real_os)
    fake.deny_listdir = True
    moved, code = under(fake, rotate, list_path, 4096, 2, state_list)
    check("relay-rotation-listing-denied-still-rotates",
          moved is True and code is None and os.path.exists(list_path + ".1"),
          "moved=%r code=%r" % (moved, code))
    check("relay-rotation-listing-denied-is-recorded",
          "ROTATION_LISTING" in gap_codes(state_list), repr(gap_codes(state_list)))

    # 5. a partial rotation (history moved, primary did not) is not a
    #    rotation; the retry really rotates and the retained set stays bounded
    retry_path = os.path.join(tmp, "rot_retry", "relay.log")
    os.makedirs(os.path.dirname(retry_path), exist_ok=True)
    write_text(retry_path + ".1", "history\n")
    write_text(retry_path, "r" * 6000)
    fake = FakeOs(real_os)
    fake.deny_replace = [lambda src, dst: os.path.basename(src) == "relay.log"]
    moved, code = under(fake, rotate, retry_path, 4096, 2)
    check("relay-rotation-partial-not-reported",
          moved is None and code == "ROTATION_PRIMARY_DENIED",
          "moved=%r code=%r" % (moved, code))
    moved2, code2 = call_out(rotate, retry_path, 4096, 2)
    names = sorted(os.listdir(os.path.dirname(retry_path)))
    check("relay-rotation-partial-then-retry-rotates",
          moved2 is True and code2 is None and os.path.exists(retry_path + ".1"),
          "moved=%r code=%r %r" % (moved2, code2, names))
    check("relay-rotation-partial-then-retry-bounded",
          names == ["relay.log.1", "relay.log.2"]
          and read_text(retry_path + ".1").startswith("r")
          and read_text(retry_path + ".2") == "history\n",
          repr(names))

    # 6. the stale-generation sweep is interrupted: the rotation still
    #    happened, the stale file survives, the refusal is recorded
    clean_dir = os.path.join(tmp, "rot_clean")
    os.makedirs(clean_dir, exist_ok=True)
    clean_path = os.path.join(clean_dir, "relay.log")
    write_text(clean_path + ".7", "stale\n")
    write_text(clean_path, "c" * 6000)
    state_clean = make_state(src, clean_path, max_bytes=4096, keep=2)
    fake = FakeOs(real_os)
    fake.deny_remove = True
    moved, code = under(fake, rotate, clean_path, 4096, 2, state_clean)
    check("relay-rotation-interrupted-cleanup-still-rotates",
          moved is True and code is None and os.path.exists(clean_path + ".1"),
          "moved=%r code=%r" % (moved, code))
    check("relay-rotation-interrupted-cleanup-is-recorded",
          "ROTATION_CLEANUP" in gap_codes(state_clean), repr(gap_codes(state_clean)))
    check("relay-rotation-interrupted-cleanup-keeps-stale",
          read_text(clean_path + ".7") == "stale\n",
          repr(sorted(os.listdir(clean_dir))))

    # 7. the normal keep values still behave: keep=0 bounds the primary,
    #    keep=1 keeps one generation, keep=2 keeps two
    keep_dir = os.path.join(tmp, "rot_keep")
    os.makedirs(keep_dir, exist_ok=True)
    one_path = os.path.join(keep_dir, "one.log")
    write_text(one_path, "1" * 6000)
    moved, code = call_out(rotate, one_path, 4096, 1)
    check("relay-rotation-keep-one",
          moved is True and code is None
          and sorted(os.listdir(keep_dir)) == ["one.log.1"],
          "moved=%r %r" % (moved, sorted(os.listdir(keep_dir))))
    two_path = os.path.join(keep_dir, "two.log")
    for _ in range(2):
        write_text(two_path, "2" * 6000)
        moved, code = call_out(rotate, two_path, 4096, 2)
    check("relay-rotation-keep-two",
          moved is True and code is None
          and sorted(os.listdir(keep_dir)) == ["one.log.1", "two.log.1", "two.log.2"],
          repr(sorted(os.listdir(keep_dir))))
    # the primary keeps its NAME: the caller writes the new primary there
    write_text(two_path, "new primary\n")
    check("relay-rotation-primary-name-stable",
          os.path.exists(two_path) and read_text(two_path) == "new primary\n",
          repr(sorted(os.listdir(keep_dir))))

    # 8. the counter must never claim a rotation that did not happen
    cnt_dir = os.path.join(tmp, "rot_count")
    os.makedirs(cnt_dir, exist_ok=True)
    cnt_path = os.path.join(cnt_dir, "relay.log")
    write_text(cnt_path + ".1", "old generation\n")
    write_text(cnt_path, "z" * 6000)
    write_text(src, "s" * 6000 + "\n")
    state_cnt = make_state(src, cnt_path, max_bytes=4096, keep=2)
    fake = FakeOs(real_os)
    fake.deny_replace = [lambda src_, dst: os.path.basename(src_) == "relay.log"]
    moved, code = under(fake, relay.poll_once, state_cnt)
    check("relay-rotation-no-false-count",
          state_cnt.get("output_rotations", 0) == 0,
          repr(state_cnt.get("output_rotations")))
    check("relay-rotation-refusal-is-named",
          code == "ROTATION_PRIMARY_DENIED" and "ROTATION_PRIMARY_DENIED" in gap_codes(state_cnt),
          "code=%r gaps=%r" % (code, gap_codes(state_cnt)))
    # --- state survives a JSON round trip (the runner persists every cycle)
    raw = json.loads(json.dumps(relay.encode_state(state_rot)))
    restored = relay.decode_state(raw)
    check("relay-state-roundtrip-preserves-pending",
          isinstance(restored["pending"], bytes) and restored["pending"] == state_rot["pending"],
          "%r" % (restored["pending"],))
    check("relay-state-roundtrip-preserves-offset",
          restored["offset"] == state_rot["offset"] and restored["identity"] == state_rot["identity"])

    # --- lease expiry, revocation and liveness
    check("relay-lease-expiry-computed",
          relay.lease_expired({"expires_at": time.time() - 1}) is True
          and relay.lease_expired({"expires_at": time.time() + 60}) is False)
    check("relay-lease-missing-expiry-is-open",
          relay.lease_expired({}) is False, "a lease without an expiry must not expire")

    # --- bounded(): the refusal path is exercised, not guessed at
    for raw, field, lo, hi in (("x", "interval", 0.05, 3600.0),
                               ("0", "interval", 0.05, 3600.0),
                               ("999999", "interval", 0.05, 3600.0),
                               ("-5", "interval", 0.05, 3600.0)):
        try:
            relay.bounded(raw, field, lo, hi)
            check("relay-bounded-refuses-%s" % raw, False, "accepted")
        except relay.RelayError as ex:
            check("relay-bounded-refuses-%s" % raw, ex.code == "INVALID_INTERVAL", ex.code)

    # --- parse_args(): unknown arguments never leak through
    try:
        relay.parse_args(["--source", src, "--exe", "calc.exe"],
                         {"--source": "source"}, [])
        check("relay-parse-refuses-unknown", False, "accepted an unknown flag")
    except relay.RelayError as ex:
        check("relay-parse-refuses-unknown", ex.code == "ARGS" and "exe" in ex.detail, ex.detail)

    try:
        relay.parse_args(["--source"], {"--source": "source"}, [])
        check("relay-parse-requires-value", False, "accepted a dangling flag")
    except relay.RelayError as ex:
        check("relay-parse-requires-value", ex.code == "ARGS", ex.detail)

    opts, seen = relay.parse_args(["--source", src, "--session"],
                                  {"--source": "source"}, ["--session"])
    check("relay-parse-accepts-flags",
          opts == {"source": src} and seen == ["--session"], repr((opts, seen)))

    # --- start refuses the shapes that matter before it spawns a runner
    monkey = relay.subprocess.Popen

    def refuse_runner(*args, **kwargs):
        # subprocess.run() calls Popen internally too, so the guard must only
        # fire for the relay runner itself: a fresh interpreter on this file.
        command = [str(c) for c in args[0]] if args and isinstance(args[0], (list, tuple)) else []
        if "run" in command and any(c.endswith("sai_relay.py") for c in command):
            raise AssertionError("start must not spawn a runner when validation fails")
        return monkey(*args, **kwargs)

    relay.subprocess.Popen = refuse_runner
    try:
        for argv, code in (
            (["--account", "ag2", "--source", src], "WORKDIR_REQUIRED"),
            (["--account", "ag2", "--source", os.path.join(tmp, "nope.log"),
              "--workdir", tmp], "SOURCE_NOT_FOUND"),
            (["--source", src, "--workdir", tmp], "ACCOUNT_REQUIRED"),
            (["--account", "ag2", "--workdir", tmp], "SOURCE_REQUIRED"),
            (["--account", "ag2", "--source", dirsrc, "--workdir", tmp], "SOURCE_UNSAFE_OBJECT"),
        ):
            try:
                relay.cmd_start(argv)
                check("relay-start-refuses-%s" % code, False, "accepted")
            except relay.RelayError as ex:
                check("relay-start-refuses-%s" % code, ex.code == code, ex.code)
            except AssertionError as ex:
                check("relay-start-refuses-%s" % code, False, "spawned a runner: %s" % ex)
    finally:
        relay.subprocess.Popen = monkey

    # --- pid_alive(): OpenProcess alone answers "some process holds this pid",
    #     which is still true after the original exited and the OS reused the
    #     number. A dead relay must report dead, or a stale runner keeps looking
    #     authoritative and status keeps claiming RUNNING.
    import subprocess as _sp

    probe = _sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    probe_pid = probe.pid
    check("relay-pid-alive-for-live-process", relay.pid_alive(probe_pid) is True,
          "a running process reported dead")
    probe.kill()
    probe.wait()
    check("relay-pid-dead-for-exited-process", relay.pid_alive(probe_pid) is False,
          "an exited pid still reported alive")
    check("relay-pid-unknown-is-dead", relay.pid_alive(0x7FFFFFF2) is False, "an unknown pid reported alive")
    check("relay-pid-self-is-alive", relay.pid_alive(os.getpid()) is True, "the relay reported itself dead")

    # --- the runner re-reads the lease every cycle: a stop issued after the
    #     runner started must terminate it, so the control directory is polled
    #     rather than cached once before the loop.
    saved_root = os.environ.get("SAI_HANDOFF_ROOT")
    os.environ["SAI_HANDOFF_ROOT"] = os.path.join(tmp, "handoff_root")
    try:
        live_src = os.path.join(tmp, "runner.log")
        write_text(live_src, "line\n")
        live_dst = os.path.join(tmp, "runner-staged.log")
        rlease = {
            "schema": relay.SCHEMA + "/lease",
            "lease_id": "runner-lease",
            "account": "ag2",
            "source": os.path.abspath(live_src),
            "staged_path": live_dst,
            "created_at": relay.now_iso(),
            "created_by_pid": os.getpid(),
            "runner_pid": None,
            "expires_at": time.time() + 3600,
            "expires_at_iso": "",
            "revoked": False,
            "generation": 1,
        }
        rstate = relay.relay_state(rlease, rlease["source"], live_dst,
                                   {"interval": 0.02, "max_bytes": 1024, "keep": 2,
                                    "missing_grace_seconds": 300})
        rcdir = relay.control_dir("ag2", rlease["source"])
        os.makedirs(rcdir, exist_ok=True)
        relay.write_json_atomic(relay.lease_path(rcdir), rlease)
        relay.write_json_atomic(relay.state_path(rcdir), relay.encode_state(rstate))

        real_read_json = relay.read_json
        reads = {"lease": 0}

        def lease_each_cycle(path):
            """The lease is live until the operator stops the relay."""
            if str(path) == relay.lease_path(rcdir):
                reads["lease"] += 1
                if reads["lease"] > 1:
                    stopped = dict(rlease)
                    stopped["revoked"] = True
                    return stopped
            return real_read_json(path)

        relay.read_json = lease_each_cycle
        real_sleep = relay.time.sleep
        guard = {"cycles": 0}

        def bounded_sleep(seconds):
            # Without this guard a regression (cached lease) loops forever.
            guard["cycles"] += 1
            if guard["cycles"] > 5:
                raise RuntimeError("runner did not observe the revoked lease")

        relay.time.sleep = bounded_sleep
        rc, detail = None, ""
        try:
            rc = relay.cmd_run(["--source", rlease["source"], "--compact", "ag2"])
        except RuntimeError as ex:
            rc, detail = None, str(ex)
        finally:
            relay.read_json = real_read_json
            relay.time.sleep = real_sleep
        check("relay-runner-re-reads-lease-and-stops",
              rc == 0 and reads["lease"] > 1,
              "rc=%r lease_reads=%d detail=%r" % (rc, reads["lease"], detail))

        stopped_status = relay.read_json(relay.status_path(rcdir))
        check("relay-runner-stop-status-stopped",
              (stopped_status or {}).get("relay_status") == "STOPPED_LEASE_REVOKED"
              and (stopped_status or {}).get("revoked") is True,
              repr((stopped_status or {}).get("relay_status")))
    finally:
        if saved_root is None:
            os.environ.pop("SAI_HANDOFF_ROOT", None)
        else:
            os.environ["SAI_HANDOFF_ROOT"] = saved_root

    # --- status/stop on an unknown relay fail loudly instead of reporting success
    for verb in ("status", "stop"):
        argv = [verb, "--account", "ag2", "--source", os.path.join(tmp, "no-such.log")]
        try:
            rc = relay.main(argv)
            check("relay-%s-unknown-relay-refuses" % verb, rc not in (0, None), "returned success")
        except relay.RelayError as ex:
            check("relay-%s-unknown-relay-refuses" % verb, ex.code == "LEASE_NOT_FOUND", ex.code)
        except SystemExit:
            check("relay-%s-unknown-relay-refuses" % verb, True)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
