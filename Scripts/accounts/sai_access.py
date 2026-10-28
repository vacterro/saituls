#!/usr/bin/env python3
"""SAI HANDOFF temporary read access -- operator-owned, non-privileged.

A narrow, expiring, read-only lease on one explicitly selected object, for the
cases where staging is unsuitable: a live file the agent must observe in place,
or a directory it must enumerate once.

What this deliberately is NOT:
  * Not a reuse of `grant-workdir`. That operation grants Modify, is owned by
    the privileged helper, and carries its own journal. Nothing here inherits
    from it, and nothing here grants Modify.
  * Not a directory-sharing API. The default is the selected object only.
    Recursion is one object's subtree, never a drive or a profile root, and it
    must be asked for by name.
  * Not a revocation guarantee for already-open Windows handles. Removing the
    ACE stops new access; a handle that is already open keeps reading until it
    closes. That limit is reported, not hidden.

Ownership and journaling mirror the helper's principles without touching
privileged code: this runs as the operator, on objects the operator owns, and
records the exact ACL state before and after the grant so a revoke removes only
what this lease demonstrably created.

stdlib only, no new dependencies. ACL writes use icacls; explicit grants are
inspected through Windows security APIs in PowerShell. Existing explicit
grants and ACLs changed after a lease are refused rather than claimed.
"""
import json
import os
import subprocess
import sys
import time
import uuid
import ctypes
import ctypes.wintypes

# GetFileAttributesW returns DWORD but ctypes defaults to c_int, so
# INVALID_FILE_ATTRIBUTES (0xFFFFFFFF) reads as -1 and an absent object looks
# like it carries every attribute -- including REPARSE_POINT, which would turn
# "not found" into "unsafe object" and report the wrong refusal.
ctypes.windll.kernel32.GetFileAttributesW.restype = ctypes.wintypes.DWORD
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF

SCHEMA = "sai.handoff/access/1"

FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_DIRECTORY = 0x00000010

VALID_MINUTES = (15, 60)


class LeaseError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code + (": " + detail if detail else ""))
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------ helpers
def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _file_attrs(path):
    try:
        return ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except Exception:
        return INVALID_FILE_ATTRIBUTES


def is_reparse_point(path):
    attrs = _file_attrs(path)
    return attrs != INVALID_FILE_ATTRIBUTES and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def is_directory(path):
    attrs = _file_attrs(path)
    return attrs != INVALID_FILE_ATTRIBUTES and bool(attrs & FILE_ATTRIBUTE_DIRECTORY)


def accounts_exe():
    override = os.environ.get("SAI_ACCOUNTS_EXE")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "accounts", "bin", "sai-accounts.exe")


def control_root():
    override = os.environ.get("SAI_HANDOFF_ROOT")
    if override:
        return os.path.join(override, "access")
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return os.path.join(base, "SAITULS", "SAI_HANDOFF", "access")


def lease_key(path):
    import hashlib
    digest = hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:12]
    # The basename is lowercased too: two leases differing only by filename case
    # would otherwise name the same object twice on a case-insensitive filesystem.
    tag = os.path.basename(path)[:40].lower().replace(" ", "_") or "obj"
    return "%s-%s" % (digest, tag)


def lease_path(compact, path):
    return os.path.join(control_root(), compact.lower(), lease_key(path) + ".json")


def write_json_atomic(path, payload):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + "." + str(os.getpid()) + "." + uuid.uuid4().hex[:8] + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def run_accounts(*args, timeout=60):
    exe = accounts_exe()
    if not os.path.exists(exe):
        raise LeaseError("ACCOUNTS_CLI_MISSING", exe)
    proc = subprocess.run([exe] + list(args), capture_output=True, text=True, timeout=timeout)
    lines = proc.stdout.strip().splitlines()
    doc = None
    if lines:
        try:
            doc = json.loads(lines[-1])
        except ValueError:
            doc = None
    return proc.returncode, doc


def resolve_account(selector):
    """Identity comes from the canonical registry. The caller never supplies a
    SID, so no caller can point a grant at an account it does not own."""
    code, doc = run_accounts("get", selector)
    if doc is None:
        raise LeaseError("ACCOUNT_LOOKUP_FAILED", selector)
    if code != 0 or doc.get("status") == "ACCOUNT_NOT_FOUND" or not doc.get("account"):
        raise LeaseError("ACCOUNT_NOT_FOUND", selector)
    account = doc["account"]
    compact = account.get("compact_label")
    sid = account.get("windows_sid")
    if not compact:
        raise LeaseError("ACCOUNT_IDENTITY_MISSING", selector)
    if not sid:
        raise LeaseError("CONTEXT_IDENTITY_MISSING",
                         "no managed Windows SID resolved for %s" % selector)
    return {"account_id": account.get("account_id"), "compact_label": compact,
            "windows_sid": sid}


# ------------------------------------------------------------------ ACL work
def acl_text(path):
    """The object's ACE list, and nothing else.

    icacls appends a "Successfully processed 1 files" summary that varies with
    the -option set, so keeping it would make a before/after comparison fail on
    the summary rather than on the DACL.
    """
    proc = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise LeaseError("ACL_READ_FAILED", (proc.stdout + proc.stderr).strip()[:300])
    keep, seen_acl = [], False
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Successfully processed"):
            continue
        if not seen_acl:
            seen_acl = True
        keep.append(stripped)
    return "\n".join(keep)


def icacls(path, verb, identity, rights=""):
    """Apply one ACL change with the platform tool.

    ``/remove`` takes a SID and no rights mask: icacls documents
    ``/remove[:[g|d]] Sid`` and a mask on the removal is rejected with 1337
    even when the ACE is present. ``:g`` keeps the removal scoped to granted
    ACEs, so a deny ACE for the same account is never touched.
    """
    if verb.startswith("/remove"):
        args = ["icacls", str(path), verb, "*%s" % identity]
    else:
        args = ["icacls", str(path), verb, "*%s:%s" % (identity, rights)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise LeaseError("ACL_MUTATION_FAILED" if verb == "/grant" else "ACL_REVOKE_FAILED",
                         (proc.stdout + proc.stderr).strip()[:300])


def has_explicit_allow(path, sid):
    """A pre-existing allow rule cannot be owned or revoked by this lease."""
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = (
        "$ErrorActionPreference='Stop'; "
        "$acl=Get-Acl -LiteralPath %s; "
        "$rules=$acl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]); "
        "[bool](@($rules | Where-Object { $_.IdentityReference.Value -eq %s "
        "-and $_.AccessControlType -eq 'Allow' }).Count) | ConvertTo-Json -Compress"
    ) % (quote(path), quote(sid))
    # A parent PowerShell 7 module path cannot be reused by Windows PowerShell.
    env = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    proc = subprocess.run(["powershell", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=60, env=env)
    try:
        answer = json.loads(proc.stdout)
    except ValueError:
        answer = None
    if proc.returncode or type(answer) is not bool:
        raise LeaseError("ACL_READ_FAILED", (proc.stdout + proc.stderr).strip()[:300])
    return answer


def remove_owned_allow(lease):
    """Refuse removal when the current ACL no longer matches the lease."""
    if acl_text(lease["source"]) != lease.get("acl_after"):
        raise LeaseError("ACL_CHANGED_SINCE_GRANT",
                         "the ACL changed since this lease; refusing to remove unowned rules")
    icacls(lease["source"], "/remove:g", lease["windows_sid"])


def ancestors_traversable(selector, target):
    """A read ACE on a nested object is unreachable unless every ancestor is
    already traversable by that account. Opening a directory already requires
    traverse on the whole chain, so one probe of the parent answers it.

    This is the check that keeps a narrow file grant from silently becoming a
    profile-wide grant: if the parent is refused, the grant would have to widen
    the tree to work at all, and that widening is refused here.
    """
    parent = os.path.dirname(os.path.abspath(target)) or os.path.abspath(target)
    code, doc = run_accounts("probe-workdir", selector, "--workdir", parent, timeout=60)
    if doc is None:
        return False, doc
    return code == 0 and doc.get("status") == "ok", doc


def verify_readable(selector, target):
    """Effective access under the managed identity, not under ours."""
    if is_directory(target):
        code, doc = run_accounts("probe-workdir", selector, "--workdir", os.path.abspath(target), timeout=60)
        if doc is None:
            return None
        return code == 0 and doc.get("status") == "ok"
    code, doc = run_accounts("verify-file", selector, "--path", os.path.abspath(target), timeout=60)
    if doc is None:
        return None
    return code == 0 and doc.get("readable") is True


# ----------------------------------------------------------------- commands
def cmd_grant(argv):
    opts, flags = parse_args(argv, {
        "--account": "account", "--source": "source", "--minutes": "minutes",
    }, ["--session", "--recursive"])
    if not opts.get("account"):
        raise LeaseError("ACCOUNT_REQUIRED", "--account <selector>")
    if not opts.get("source"):
        raise LeaseError("SOURCE_REQUIRED", "--source <path>")

    target = os.path.abspath(opts["source"])
    selector = opts["account"]

    if is_reparse_point(target):
        raise LeaseError("REPARSE_POINT_REFUSED",
                         "a reparse point is not the object the operator selected: " + target)
    if not os.path.exists(target):
        raise LeaseError("SOURCE_NOT_FOUND", target)

    account = resolve_account(selector)
    compact = account["compact_label"]
    sid = account["windows_sid"]

    traversable, probe = ancestors_traversable(selector, target)
    if not traversable:
        # Refuse rather than widen: granting traverse up to a profile root is
        # exactly the broad expansion this feature exists to avoid. The staged
        # relay is the correct answer here, and the operator is told that.
        raise LeaseError("ANCESTOR_NOT_TRAVERSABLE",
                         "the account cannot reach the parent directory (%s). Granting traverse "
                         "would broaden access beyond the selection; use sai_relay for a staged "
                         "copy instead." % (probe.get("reason") if probe else "no probe"))

    if "--session" in flags:
        expires_at, expires_at_iso = 0, ""
    elif opts.get("minutes") is not None:
        try:
            minutes = int(opts["minutes"])
        except ValueError:
            raise LeaseError("INVALID_MINUTES", str(opts["minutes"]))
        if minutes not in VALID_MINUTES:
            raise LeaseError("INVALID_MINUTES",
                             "supported durations are %s minutes, or --session"
                             % ", ".join(str(v) for v in VALID_MINUTES))
        expires_at = time.time() + minutes * 60
        expires_at_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(expires_at))
    else:
        raise LeaseError("DURATION_REQUIRED", "name --minutes 15, --minutes 60 or --session")

    path = lease_path(compact, target)
    existing = read_json(path)
    if existing and not existing.get("revoked") and not lease_expired(existing):
        raise LeaseError("LEASE_ALREADY_ACTIVE", existing.get("lease_id"))

    recursive = "--recursive" in flags and is_directory(target)
    if "--recursive" in flags and not is_directory(target):
        raise LeaseError("RECURSIVE_ONLY_FOR_DIRECTORIES", target)

    if has_explicit_allow(target, sid):
        raise LeaseError("EQUIVALENT_ACE_ALREADY_PRESENT",
                         "an explicit allow rule already exists for this SID; a lease cannot own it")
    before = acl_text(target)
    if recursive:
        rights = "(OI)(CI)(R)"
    elif is_directory(target):
        # Files directly inside this one directory; subdirectories are not
        # descended into unless the operator asks for recursion.
        rights = "(OI)(R)"
    else:
        rights = "(R)"
    icacls(target, "/grant", sid, rights)
    after = acl_text(target)

    if after == before:
        # An equivalent ACE was already present. It is indistinguishable from
        # one this lease would create, so a revoke could remove a foreign rule:
        # the lease claims nothing and says so.
        raise LeaseError("EQUIVALENT_ACE_ALREADY_PRESENT",
                         "a read ACE for this SID is already on the object; this lease cannot "
                         "prove ownership of it and therefore grants nothing")

    lease = {
        "schema": SCHEMA + "/lease",
        "lease_id": uuid.uuid4().hex,
        "account": compact,
        "account_id": account["account_id"],
        "windows_sid": sid,
        "source": target,
        "object_kind": "directory" if is_directory(target) else "file",
        "recursive": bool(recursive),
        "rights": rights,
        "acl_before": before,
        "acl_after": after,
        "created_at": now_iso(),
        "expires_at": expires_at,
        "expires_at_iso": expires_at_iso,
        "revoked": False,
        "revoked_at": "",
    }
    write_json_atomic(path, lease)
    doc = dict(lease)
    doc["status"] = "ok"
    doc["schema"] = SCHEMA + "/grant"
    doc["verified_readable"] = verify_readable(selector, target)
    doc["expiry_caveat"] = ("an already-open Windows handle keeps reading after expiry; this "
                            "lease bounds new access only")
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


def cmd_revoke(argv):
    opts, _flags = parse_args(argv, {"--account": "account", "--source": "source"}, [])
    if not opts.get("account") or not opts.get("source"):
        raise LeaseError("ACCOUNT_AND_SOURCE_REQUIRED", "--account <selector> --source <path>")
    account = resolve_account(opts["account"])
    path = lease_path(account["compact_label"], os.path.abspath(opts["source"]))
    lease = read_json(path)
    if lease is None:
        raise LeaseError("LEASE_NOT_FOUND", os.path.abspath(opts["source"]))
    if lease.get("revoked"):
        raise LeaseError("LEASE_ALREADY_REVOKED", lease.get("lease_id"))

    target = lease["source"]
    before = acl_text(target)
    remove_owned_allow(lease)
    after = acl_text(target)

    if after != lease.get("acl_before"):
        # Rollback did not restore the recorded state. The evidence is kept and
        # the discrepancy is reported rather than smoothed into a success.
        lease["revoked"] = True
        lease["revoked_at"] = now_iso()
        lease["rollback_mismatch"] = True
        lease["acl_after_revoke"] = after
        write_json_atomic(path, lease)
        raise LeaseError("ROLLBACK_MISMATCH",
                         "the ACL after revoke differs from the recorded pre-grant state; the "
                         "lease is marked revoked and the discrepancy is preserved")

    lease["revoked"] = True
    lease["revoked_at"] = now_iso()
    write_json_atomic(path, lease)
    doc = dict(lease)
    doc["status"] = "ok"
    doc["schema"] = SCHEMA + "/revoke"
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


def cmd_status(argv):
    opts, _flags = parse_args(argv, {"--account": "account", "--source": "source"}, [])
    if not opts.get("account") or not opts.get("source"):
        raise LeaseError("ACCOUNT_AND_SOURCE_REQUIRED", "--account <selector> --source <path>")
    account = resolve_account(opts["account"])
    path = lease_path(account["compact_label"], os.path.abspath(opts["source"]))
    lease = read_json(path)
    if lease is None:
        raise LeaseError("LEASE_NOT_FOUND", os.path.abspath(opts["source"]))
    doc = dict(lease)
    doc["status"] = "ok"
    doc["schema"] = SCHEMA + "/status"
    doc["expired"] = lease_expired(lease)
    doc["verified_readable"] = verify_readable(opts["account"], lease["source"])
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


def cmd_sweep(argv):
    """Revoke every expired lease this journal provably owns.

    Expiration is enforced here. It is NOT enforced against already-open
    Windows handles: an open handle keeps reading until it closes, so a sweep
    bounds new access, and the operator is told that instead of being led to
    expect otherwise.
    """
    opts, _flags = parse_args(argv, {}, [])
    revoked, failures = [], []
    root = control_root()
    if os.path.isdir(root):
        for compact in sorted(os.listdir(root)):
            cdir = os.path.join(root, compact)
            if not os.path.isdir(cdir):
                continue
            for name in sorted(os.listdir(cdir)):
                lease = read_json(os.path.join(cdir, name))
                if lease is None or lease.get("revoked") or not lease_expired(lease):
                    continue
                try:
                    remove_owned_allow(lease)
                except LeaseError as ex:
                    # A lease that cannot be revoked stays in the journal and is
                    # reported. It is never silently dropped.
                    failures.append({"lease_id": lease.get("lease_id"), "reason": ex.code})
                    continue
                lease["revoked"] = True
                lease["revoked_at"] = now_iso()
                write_json_atomic(os.path.join(cdir, name), lease)
                revoked.append(lease["lease_id"])
    print(json.dumps({"schema": SCHEMA + "/sweep", "status": "ok",
                      "revoked": revoked, "unrevoked": failures,
                      "caveat": "already-open Windows handles are unaffected by revoke"},
                     indent=2))
    return 0


def cmd_list(argv):
    parse_args(argv, {}, [])
    rows = []
    root = control_root()
    if os.path.isdir(root):
        for compact in sorted(os.listdir(root)):
            cdir = os.path.join(root, compact)
            if not os.path.isdir(cdir):
                continue
            for name in sorted(os.listdir(cdir)):
                lease = read_json(os.path.join(cdir, name))
                if lease is None:
                    continue
                rows.append({
                    "account": lease.get("account"),
                    "source": lease.get("source"),
                    "object_kind": lease.get("object_kind"),
                    "rights": lease.get("rights"),
                    "recursive": lease.get("recursive"),
                    "expires_at": lease.get("expires_at_iso"),
                    "revoked": bool(lease.get("revoked")),
                    "expired": lease_expired(lease),
                })
    print(json.dumps({"schema": SCHEMA + "/list", "status": "ok", "leases": rows}, indent=2))
    return 0


# ------------------------------------------------------------------- plumbing
def parse_args(argv, valued, flags):
    opts, seen_flags, unknown = {}, [], []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in valued:
            if index + 1 >= len(argv):
                raise LeaseError("ARGS", "%s needs a value" % token)
            opts[valued[token]] = argv[index + 1]
            index += 2
            continue
        if token in flags:
            seen_flags.append(token)
            index += 1
            continue
        unknown.append(token)
        index += 1
    if unknown:
        raise LeaseError("ARGS", "unknown argument: %s" % unknown[0])
    return opts, seen_flags


def lease_expired(lease):
    expires = lease.get("expires_at")
    if not expires:
        return False
    try:
        return time.time() >= expires
    except (TypeError, ValueError):
        return True


def main(argv):
    if not argv:
        print(json.dumps({"status": "USAGE",
                          "commands": ["grant", "revoke", "status", "sweep", "list"]}, indent=2))
        return 2
    verb, rest = argv[0], argv[1:]
    handlers = {"grant": cmd_grant, "revoke": cmd_revoke, "status": cmd_status,
                "sweep": cmd_sweep, "list": cmd_list}
    handler = handlers.get(verb)
    if handler is None:
        print(json.dumps({"status": "UNKNOWN_COMMAND", "command": verb}, indent=2))
        return 2
    try:
        return handler(rest)
    except LeaseError as ex:
        print(json.dumps({"status": ex.code, "detail": ex.detail,
                          "schema": SCHEMA + "/error"}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
