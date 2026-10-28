"""Temporary read-access lease checks (test_sai_access.py).

The account registry is replaced by a stub that only speaks the three verbs this
module is allowed to use (get / probe-workdir / verify-file), and every verb it
receives is recorded so the test can prove no privileged verb was ever asked
for. icacls runs for real, against disposable objects in a temp directory, under
the operator's own identity: the ACE written and removed is one this lease
created, and nothing else is touched.

Run: python tests/test_sai_access.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS)

STUB = r'''# -*- coding: utf-8 -*-
"""Fake sai-accounts registry. Records every verb; answers only what it is asked."""
import json, os, sys

IDENTITY = os.environ["FAKE_IDENTITY"]
TRAVERSABLE = os.environ["FAKE_TRAVERSABLE"].split(";") if os.environ["FAKE_TRAVERSABLE"] else []
LOG = os.environ["FAKE_VERB_LOG"]

def log(verb):
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(verb + "\n")

def out(doc, code=0):
    print(json.dumps(doc, sort_keys=True))
    return code

args = sys.argv[1:]
verb = args[0] if args else ""
log(verb)

if verb == "get":
    selector = args[1] if len(args) > 1 else ""
    if selector.startswith("nope"):
        raise SystemExit(out({"status": "ACCOUNT_NOT_FOUND", "account": None}, 1))
    raise SystemExit(out({"status": "ok",
                          "account": {"account_id": "AG-test",
                                      "compact_label": "agtest",
                                      "windows_sid": IDENTITY}}))
if verb == "probe-workdir":
    target = args[args.index("--workdir") + 1] if "--workdir" in args else ""
    ok = any(os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(t))
             for t in TRAVERSABLE)
    if ok:
        raise SystemExit(out({"status": "ok", "workdir": target}))
    raise SystemExit(out({"status": "CONTEXT_OFFLINE", "reason": "managed token cannot enter this directory"}, 1))
if verb == "verify-file":
    raise SystemExit(out({"status": "ok", "readable": True}))
raise SystemExit(out({"status": "UNKNOWN_STUB_VERB", "verb": verb}, 1))
'''


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


access = load_module("sai_access", os.path.join(REPO, "Scripts", "accounts", "sai_access.py"))

PASS = []
FAIL = []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
    print("%s  %s%s" % ("PASS" if condition else "FAIL", name,
                        "" if condition else "  " + str(detail)))


def read_verbs():
    if not os.path.exists(VERB_LOG):
        return []
    with open(VERB_LOG, encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def clear_verbs():
    if os.path.exists(VERB_LOG):
        os.remove(VERB_LOG)


def grant(argv):
    """Run a grant the way an operator does: through the CLI entry point."""
    return capture(["grant"] + argv)


def capture(argv):
    """Run a command, capture its printed JSON document (errors are printed,
    not raised, because the CLI is a CLI)."""
    import io
    import contextlib
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = access.main(argv)
    text = buffer.getvalue().strip()
    try:
        doc = json.loads(text)
    except ValueError:
        doc = {}
    return code, doc


def main():
    tmp = tempfile.mkdtemp(prefix="sai_access_")
    stub = os.path.join(tmp, "fake_accounts.py")
    with open(stub, "w", encoding="utf-8") as handle:
        handle.write(STUB)
    os.environ["SAI_ACCOUNTS_EXE"] = sys.executable
    # The stub is invoked as `<python> <stub> <verb> ...`, so wrap it in a .cmd
    # that prepends the interpreter.
    wrapper = os.path.join(tmp, "fake_accounts.cmd")
    with open(wrapper, "w", encoding="utf-8") as handle:
        handle.write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, stub))
    os.environ["SAI_ACCOUNTS_EXE"] = wrapper
    # icacls takes a *SID with the * prefix; resolve the real operator SID so
    # the grant under test is a real, revocable ACE on a disposable object.
    identity = subprocess.run(["powershell", "-NoProfile", "-Command",
                               "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
                              capture_output=True, text=True).stdout.strip()
    if not identity.startswith("S-1-5-"):
        raise SystemExit("could not resolve an operator SID for the ACL checks: %r" % identity)
    os.environ["FAKE_IDENTITY"] = identity
    global VERB_LOG
    VERB_LOG = os.path.join(tmp, "verbs.log")
    os.environ["FAKE_VERB_LOG"] = VERB_LOG
    os.environ["SAI_HANDOFF_ROOT"] = os.path.join(tmp, "control")
    workdir = os.path.join(tmp, "share")
    os.makedirs(workdir, exist_ok=True)
    os.environ["FAKE_TRAVERSABLE"] = workdir

    target = os.path.join(workdir, "live.log")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("hello\n")

    # --- argument and selection refusals
    for argv, code in (
        (["--source", target], "ACCOUNT_REQUIRED"),
        (["--account", "ag2"], "SOURCE_REQUIRED"),
        (["--account", "ag2", "--source", os.path.join(workdir, "gone.log"), "--minutes", "15"],
         "SOURCE_NOT_FOUND"),
        (["--account", "ag2", "--source", target], "DURATION_REQUIRED"),
        (["--account", "ag2", "--source", target, "--minutes", "5"], "INVALID_MINUTES"),
        (["--account", "ag2", "--source", target, "--minutes", "abc"], "INVALID_MINUTES"),
        (["--account", "nope-account", "--source", target, "--minutes", "15"], "ACCOUNT_NOT_FOUND"),
        (["--account", "ag2", "--source", target, "--minutes", "15", "--exe", "calc.exe"],
         "ARGS"),
    ):
        clear_verbs()
        rc, doc = grant(argv)
        check("access-refuses-%s" % code, rc != 0 and doc.get("status") == code,
              (rc, doc))

    # --- a target outside the traversable chain is refused, not widened
    outside = os.path.join(tmp, "Documents", "secret.log")
    os.makedirs(os.path.dirname(outside), exist_ok=True)
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("secret\n")
    clear_verbs()
    rc, doc = grant(["--account", "ag2", "--source", outside, "--minutes", "15"])
    check("access-refuses-unreachable-parent",
          rc != 0 and doc.get("status") == "ANCESTOR_NOT_TRAVERSABLE", (rc, doc))
    check("access-never-grants-traverse", read_verbs() == ["get", "probe-workdir"],
          read_verbs())

    # --- recursion is refused for a file
    rc, doc = grant(["--account", "ag2", "--source", target, "--minutes", "15", "--recursive"])
    check("access-refuses-recursive-on-file",
          rc != 0 and doc.get("status") == "RECURSIVE_ONLY_FOR_DIRECTORIES", (rc, doc))

    # --- a reparse point is refused, not followed
    monkey = access.is_reparse_point
    access.is_reparse_point = lambda path: path.endswith("link.log")
    rc, doc = grant(["--account", "ag2", "--source", os.path.join(workdir, "link.log"),
                     "--minutes", "15"])
    check("access-refuses-reparse-point",
          rc != 0 and doc.get("status") == "REPARSE_POINT_REFUSED", (rc, doc))
    access.is_reparse_point = monkey

    # --- a successful grant records the exact ACL before and after
    before = access.acl_text(target)
    code, doc = capture(["grant", "--account", "ag2", "--source", target, "--minutes", "15"])
    check("access-grant-succeeds", code == 0 and doc.get("status") == "ok", doc)
    check("access-grant-records-acl-before", doc.get("acl_before") == before,
          "recorded the wrong baseline")
    after = access.acl_text(target)
    check("access-grant-records-acl-after", doc.get("acl_after") == after,
          "recorded the wrong result")
    # icacls prints the account's SID under its resolved name, so the ACE is
    # identified by the read right this lease granted, not by the SID string.
    check("access-grant-adds-read-ace", ":(R)" in after and ":(R)" not in before,
          (before, after))
    check("access-grant-is-not-modify",
          ":(M)" not in after and ":(F)" not in after, after)
    check("access-grant-expiry-set", doc.get("expires_at") and doc.get("expires_at_iso"),
          doc.get("expires_at_iso"))
    check("access-grant-caveats-open-handles", "already-open" in doc.get("expiry_caveat", ""),
          doc.get("expiry_caveat"))
    lease_file = access.lease_path("agtest", target)
    check("access-lease-persisted", os.path.exists(lease_file), lease_file)

    # --- revoke removes exactly the ACE it created and restores the baseline
    code, doc = capture(["revoke", "--account", "ag2", "--source", target])
    check("access-revoke-succeeds", code == 0 and doc.get("status") == "ok", doc)
    check("access-revoke-restores-baseline", access.acl_text(target) == before,
          "the pre-grant ACL was not restored")
    check("access-revoke-is-idempotent-failure",
          capture(["revoke", "--account", "ag2", "--source", target])[0] == 1, "")
    code, second = capture(["revoke", "--account", "ag2", "--source", target])
    check("access-revoke-twice-refuses", second.get("status") == "LEASE_ALREADY_REVOKED",
          second)

    # --- a foreign ACE that looks identical is never claimed
    access.icacls(target, "/grant", os.environ["FAKE_IDENTITY"], "(R)")
    foreign = access.acl_text(target)
    rc, doc = grant(["--account", "ag2", "--source", target, "--minutes", "15"])
    check("access-refuses-indistinguishable-ace",
          rc != 0 and doc.get("status") == "EQUIVALENT_ACE_ALREADY_PRESENT", (rc, doc))
    access.icacls(target, "/remove", os.environ["FAKE_IDENTITY"], "(R)")
    check("access-foreign-ace-untouched-by-refusal", access.acl_text(target) == before,
          "a refused grant still mutated the object")

    # --- rollback mismatch is preserved as evidence, never smoothed over
    capture(["grant", "--account", "ag2", "--source", target, "--minutes", "60"])
    tampered = access.read_json(lease_file)
    tampered["acl_before"] = "TAMPERED BASELINE\n"
    access.write_json_atomic(lease_file, tampered)
    code, doc = capture(["revoke", "--account", "ag2", "--source", target])
    check("access-rollback-mismatch-reported", doc.get("status") == "ROLLBACK_MISMATCH", doc)
    kept = access.read_json(lease_file)
    check("access-rollback-mismatch-marked-revoked", kept.get("revoked") is True, kept)
    check("access-rollback-mismatch-keeps-evidence",
          kept.get("rollback_mismatch") is True and kept.get("acl_after_revoke"), kept)

    # --- directory grant scope: one directory's files, not a subtree
    d = os.path.join(workdir, "dir")
    os.makedirs(os.path.join(d, "sub"), exist_ok=True)
    nested = os.path.join(d, "sub", "deep.log")
    with open(nested, "w", encoding="utf-8") as handle:
        handle.write("deep\n")
    code, doc = capture(["grant", "--account", "ag2", "--source", d, "--session"])
    check("access-directory-grant-file-inherited-only",
          "(OI)(R)" in doc.get("rights", ""), doc.get("rights"))
    icacls_out = access.acl_text(d)
    check("access-directory-grant-no-inheritance-flag",
          "(OI)(CI)" not in doc.get("rights", ""), doc.get("rights"))
    capture(["revoke", "--account", "ag2", "--source", d])
    rc, doc = capture(["grant", "--account", "ag2", "--source", d, "--session", "--recursive"])
    check("access-directory-grant-recursive-explicit",
          rc == 0 and "(OI)(CI)(R)" in doc.get("rights", ""), (rc, doc))
    capture(["revoke", "--account", "ag2", "--source", d])

    # --- a session lease never expires by the clock
    session = access.read_json(access.lease_path("agtest", d))
    check("access-session-lease-has-no-expiry", session.get("expires_at") == 0, session)
    check("access-session-lease-not-expired", access.lease_expired(session) is False, "")
    check("access-session-lease-no-iso", session.get("expires_at_iso") == "", session)

    # --- sweep enforces expiry, and says what it does not enforce
    expired_target = os.path.join(workdir, "expired.log")
    with open(expired_target, "w", encoding="utf-8") as handle:
        handle.write("x\n")
    capture(["grant", "--account", "ag2", "--source", expired_target, "--minutes", "60"])
    lease = access.read_json(access.lease_path("agtest", expired_target))
    lease["expires_at"] = 1  # already in the past
    access.write_json_atomic(access.lease_path("agtest", expired_target), lease)
    code, doc = capture(["sweep"])
    check("access-sweep-revokes-expired",
          lease["lease_id"] in doc.get("revoked", []), doc)
    check("access-sweep-caveats-open-handles",
          "already-open" in doc.get("caveat", ""), doc.get("caveat"))

    # --- the registry is never asked to mutate anything
    check("access-uses-no-privileged-verb",
          set(read_verbs()) <= {"get", "probe-workdir", "verify-file"}, set(read_verbs()))
    check("access-never-asks-grant-workdir", "grant-workdir" not in read_verbs(),
          read_verbs())

    # --- list and status report what the journal holds
    code, doc = capture(["list"])
    check("access-list-reports-leases", code == 0 and isinstance(doc.get("leases"), list), doc)
    code, doc = capture(["status", "--account", "ag2", "--source", expired_target])
    check("access-status-reports-lease", code == 0 and doc.get("lease_id"), doc)
    code, doc = capture(["status", "--account", "ag2",
                         "--source", os.path.join(workdir, "never.log")])
    check("access-status-unknown-refuses", doc.get("status") == "LEASE_NOT_FOUND", doc)

    # --- lease keys are stable and case/separator insensitive
    a = access.lease_key(os.path.join(workdir, "APP.log"))
    b = access.lease_key(os.path.join(workdir, "app.log"))
    check("access-lease-key-case-insensitive", a == b, (a, b))
    check("access-lease-key-stable", a == access.lease_key(os.path.join(workdir, "APP.LOG")),
          (a, access.lease_key(os.path.join(workdir, "APP.LOG"))))

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
