"""Capacity truth and the guarded grow path for vault containers (T-256).

Two halves, and the boundary between them is the whole point:

  * READ half -- report what actually *is*. Every number has a named source,
    and a number that cannot be measured is ``None`` ("unknown"), never a
    fabricated zero. Six logically different properties are kept apart
    because collapsing them is how a capacity display starts lying:

      ================================================  ====================
      property                                          source
      ================================================  ====================
      ``file_length_bytes``   logical file length        NTFS st_size
      ``allocated_bytes``     what the file occupies     GetCompressedFileSizeW
                                                         (VHDX: PhysicalSize)
      ``virtual_capacity_gb`` the VHDX's provisioned     virtdisk.dll
                              maximum the volume can     GetVirtualDiskInformation
                              grow into                  (SIZE).VirtualSize
      ``internal_*``          the BitLocker filesystem's needs an unlocked
                              own total/free             volume: never guessed
      ``host_*``              the volume hosting the      shutil.disk_usage
                              container file
      ``provisioned_gb``      what ``secure_apps.json``   configuration, NOT a
                              was *asked* for             measurement
      ================================================  ====================

    A freshly created 16 GiB dynamic VHDX is a 4 MiB file whose VirtualSize is
    16 GiB. Reporting either number as the other is the confirmed defect this
    module was rebuilt to remove. Nothing here mounts anything.

  * WRITE half -- grow an idle container's virtual size, and only after the
    caller has proved, in the operator's own session, that the vault is idle
    and the operator consents. The plan is printed long before anything runs;
    :func:`execute_grow` refuses everything :func:`plan_grow` refuses.

The resize verb is the documented file-oriented VHDX operation,
``Resize-VHD -Path <container> -SizeBytes <n>``, from the Hyper-V PowerShell
module. ``Resize-VirtualDisk`` is *not* it: that cmdlet acts on a disk already
attached to a VM. When the Hyper-V module is absent there is no supported
toolchain on this host and growth is refused outright -- never approximated,
never silently skipped, never accompanied by an automatic feature install or
a reboot request.
"""
import datetime
import json
import ntpath
import os
import re
import shutil
import subprocess

try:
    import sa_win
except Exception:                                    # pragma: no cover - host gap
    sa_win = None

WARN_USED_RATIO = 0.80
WARN_MIN_FREE_GB = 5.0
GROW_MIN_FREE_GB = 2.0

#: VHDX maximum virtual disk size is 64 TiB; anything above this is nonsense.
MAX_GROW_GB = 65536
#: A delta below this is not a growth this product will reason about.
MIN_GROW_DELTA_GB = 1

RETURN_OK = "ok"
RETURN_REFUSED = "refused"
RETURN_BLOCKED = "blocked"
RETURN_UNKNOWN = "unknown"

#: VHDX file identifier / VHD footer magic, read straight from the file.
_MAGIC_VHDX = b"vhdxfile"
_MAGIC_VHD = b"conectix"

_DRIVE_RE = re.compile(r"^[A-Za-z]:$")

_TOOLCHAIN_CACHE = {}


class CapacityError(Exception):
    category = "capacity"


def _gb(value):
    return value / (1024 ** 3)


# --------------------------------------------------------------------------
# host volume identity
# --------------------------------------------------------------------------
def host_volume_of(path):
    """The volume that hosts *path*, or None when no volume can be named.

    ``ntpath.splitdrive`` ALREADY returns the drive together with its colon
    (``"C:"``), so the previous ``drive + ":"`` produced ``"C::"`` -- a path
    that resolves nowhere. The same mistake turned a UNC path's share root
    (``\\\\server\\share``) into a fabricated ``\\\\server\\share:``. Both are
    fixed here, and the extended-length and device namespaces are named
    honestly rather than guessed at.

    Returns ``"C:"`` for a drive-letter path (drive root or nested, existing
    or not, fixed or removable -- the drive is the drive), the share root for
    a UNC path, and ``None`` for a device path or anything with no volume.
    """
    if not path or not isinstance(path, str):
        return None
    resolved = ntpath.abspath(path)
    drive, _ = ntpath.splitdrive(resolved)
    if not drive:
        return None
    if _DRIVE_RE.match(drive):
        return drive.upper()
    if drive.startswith("\\\\?\\"):
        inner = drive[4:]
        if _DRIVE_RE.match(inner):
            return inner.upper()
        if inner.upper().startswith("UNC\\"):
            return "\\\\" + inner[4:]
        return None
    if drive.startswith("\\\\.\\") or drive.startswith("\\\\."):
        return None                      # device namespace, not a volume
    if drive.startswith("\\\\"):
        return drive                     # UNC share root: no drive letter, no colon
    return None


def _volume_root(volume):
    """A path that unambiguously names *volume* for ``os.stat``.

    ``"C:"`` alone means "the current directory on drive C:" to Windows, so
    the separator is not cosmetic; a UNC share root gets one too.
    """
    if not volume:
        return None
    if volume.endswith("\\") or volume.endswith("/"):
        return volume
    return volume + "\\"


def image_magic(path):
    """``"vhdx"``, ``"vhd"``, ``"invalid"`` or None when the file is unreadable."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(8)
    except OSError:
        return None
    if head == _MAGIC_VHDX:
        return "vhdx"
    if head == _MAGIC_VHD:
        return "vhd"
    return "invalid"


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------
def _blank_facts(container_path, provisioned_gb):
    return {
        "container": container_path,
        "exists": False,
        # configured in secure_apps.json -- explicitly not a measurement
        "provisioned_gb": provisioned_gb,
        "provisioned_source": "secure_apps.json storage.size_gb (configured)",
        "file_length_bytes": None,
        "file_length_gb": None,
        "allocated_bytes": None,
        "allocated_gb": None,
        "virtual_capacity_bytes": None,
        "virtual_capacity_gb": None,
        "virtual_disk_block_size_bytes": None,
        "is_expandable": None,
        "format": None,
        "format_valid": None,
        "loaded": None,
        # the BitLocker filesystem's own numbers need an unlocked volume; the
        # read-only probe never mounts and so never invents them
        "internal_total_gb": None,
        "internal_free_gb": None,
        "internal_used_gb": None,
        "host_volume": None,
        "host_total_gb": None,
        "host_free_gb": None,
        "host_used_ratio": None,
        "measured_at": None,
        "sources": {},
        "errors": [],
    }


def _measure_file(facts, path):
    try:
        st = os.stat(path)
    except OSError as exc:
        facts["errors"].append({"field": "file_length_gb", "message": str(exc)})
        return
    facts["file_length_bytes"] = int(st.st_size)
    # three decimals: a 4 MiB dynamic VHDX must read 0.004 GiB, not 0.0
    facts["file_length_gb"] = round(_gb(st.st_size), 3)
    facts["sources"]["file_length_gb"] = "NTFS st_size (logical length)"
    if sa_win is None:
        return
    allocated = sa_win.allocated_bytes(path)
    if allocated is not None:
        facts["allocated_bytes"] = int(allocated)
        facts["allocated_gb"] = round(_gb(allocated), 3)
        facts["sources"]["allocated_gb"] = "GetCompressedFileSizeW (on-disk allocation)"


def _measure_virtual_disk(facts, path):
    magic = image_magic(path)
    facts["format"] = magic
    facts["sources"]["format"] = "file identifier bytes"
    if magic == "invalid":
        facts["format_valid"] = False
        facts["errors"].append(
            {"field": "format_valid",
             "message": "file identifier is not a VHD/VHDX signature"})
        return
    if magic is None:
        facts["errors"].append({"field": "format_valid",
                                "message": "container header could not be read"})
        return
    if sa_win is None:
        facts["errors"].append({"field": "virtual_capacity_gb",
                                "message": "sa_win/virtdisk unavailable on this host"})
        return
    try:
        info = sa_win.virtual_disk_info(path)
    except Exception as exc:
        # the file *looks* like a VHD/X but the disk API would not open it:
        # report unknown rather than claim a capacity that was never read
        facts["errors"].append({"field": "virtual_capacity_gb", "message": str(exc)})
        return
    facts["format_valid"] = True
    virtual = int(info.get("virtual_size_bytes") or 0)
    physical = int(info.get("physical_size_bytes") or 0)
    facts["virtual_capacity_bytes"] = virtual
    facts["virtual_capacity_gb"] = round(_gb(virtual), 2)
    facts["allocated_bytes"] = physical
    facts["allocated_gb"] = round(_gb(physical), 3)
    facts["virtual_disk_block_size_bytes"] = int(info.get("block_size_bytes") or 0)
    facts["loaded"] = bool(info.get("is_loaded"))
    facts["is_expandable"] = bool(physical < virtual)
    facts["sources"]["virtual_capacity_gb"] = \
        "virtdisk:GetVirtualDiskInformation(SIZE).VirtualSize"
    facts["sources"]["allocated_gb"] = \
        "virtdisk:GetVirtualDiskInformation(SIZE).PhysicalSize"
    facts["sources"]["is_expandable"] = "PhysicalSize < VirtualSize"
    facts["sources"]["loaded"] = \
        "virtdisk:GetVirtualDiskInformation(IS_LOADED)"


def _measure_host(facts):
    volume = host_volume_of(facts["container"])
    facts["host_volume"] = volume
    facts["sources"]["host_volume"] = "ntpath.splitdrive(abspath(container))"
    root = _volume_root(volume)
    if not root:
        facts["errors"].append(
            {"field": "host_free_gb",
             "message": "no measurable host volume for this container path"})
        return
    try:
        total, used, free = shutil.disk_usage(root)
    except (OSError, ValueError) as exc:
        facts["errors"].append({"field": "host_free_gb", "message": str(exc)})
        return
    facts["host_total_gb"] = round(_gb(total), 2)
    facts["host_free_gb"] = round(_gb(free), 2)
    facts["host_used_ratio"] = round(used / total, 3) if total else None
    facts["sources"]["host_free_gb"] = "shutil.disk_usage(%r)" % root


def container_facts(container_path, provisioned_gb):
    """Measured facts about a container file. Mounts nothing, needs no admin.

    Missing container, unreadable header, unavailable virtdisk and
    unmeasurable host free space are all reported as their truth; a caller
    renders ``None`` as "unknown" and never as zero.
    """
    facts = _blank_facts(container_path, provisioned_gb)
    if not container_path or not isinstance(container_path, str):
        facts["errors"].append({"field": "container",
                                "message": "no container path in the profile"})
        return facts
    if not os.path.isfile(container_path):
        facts["errors"].append({"field": "container",
                                "message": "no file at %s" % container_path})
        return facts
    facts["exists"] = True
    facts["measured_at"] = datetime.datetime.now().replace(
        microsecond=0).isoformat(sep=" ")
    _measure_file(facts, container_path)
    _measure_virtual_disk(facts, container_path)
    _measure_host(facts)
    return facts


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------
def classify(facts):
    """Verdict strings the GUI/CLI render verbatim; unknown stays unknown."""
    verdicts = []
    if not facts.get("exists"):
        return ["missing"]
    if facts.get("format_valid") is False:
        verdicts.append("container_format_invalid")
    if facts.get("loaded") is True:
        verdicts.append("container_attached")
    free = facts.get("host_free_gb")
    used_ratio = facts.get("host_used_ratio")
    if facts.get("host_volume") is None:
        verdicts.append("host_volume_unknown")
    if free is None:
        verdicts.append("host_free_unknown")
    elif free < GROW_MIN_FREE_GB:
        verdicts.append("grow_blocked_host_full")
    elif free < WARN_MIN_FREE_GB or (
            used_ratio is not None and used_ratio >= WARN_USED_RATIO):
        verdicts.append("host_space_warning")
    else:
        verdicts.append("host_space_ok")
    if facts.get("virtual_capacity_gb") is None:
        verdicts.append("virtual_capacity_unknown")
    logical = facts.get("file_length_gb")
    capacity = facts.get("virtual_capacity_gb")
    if logical is not None and capacity:
        if logical > capacity * 1.05:
            verdicts.append("container_over_capacity")
        elif logical < capacity * 0.60:
            verdicts.append("container_sparse")
    return verdicts


# --------------------------------------------------------------------------
# toolchain + attachment state
# --------------------------------------------------------------------------
_TOOLCHAIN_PS = (
    "$r = Get-Command Resize-VHD -ErrorAction SilentlyContinue; "
    "$g = Get-Command Get-VHD -ErrorAction SilentlyContinue; "
    "'resize-vhd=' + [bool]$r + ';get-vhd=' + [bool]$g"
)


def hyperv_toolchain(use_cache=True):
    """Is the documented VHDX resize toolchain present on this host?

    ``Resize-VHD`` (file-oriented) is the operation this product needs;
    ``Get-VHD`` is only used to corroborate attachment state. Neither is
    installed on demand and no reboot is ever requested: an absent module is
    a refusal, not a project.
    """
    if use_cache and "toolchain" in _TOOLCHAIN_CACHE:
        return dict(_TOOLCHAIN_CACHE["toolchain"])
    result = {"powershell": False, "resize_vhd": False, "get_vhd": False,
              "available": False, "detail": ""}
    if os.name != "nt":
        result["detail"] = "not a Windows host"
        _TOOLCHAIN_CACHE["toolchain"] = dict(result)
        return result
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _TOOLCHAIN_PS],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        result["detail"] = "PowerShell unavailable: %s" % exc
        _TOOLCHAIN_CACHE["toolchain"] = dict(result)
        return result
    if proc.returncode != 0:
        result["detail"] = ("PowerShell probe exited %s"
                            % proc.returncode)
        _TOOLCHAIN_CACHE["toolchain"] = dict(result)
        return result
    result["powershell"] = True
    out = proc.stdout.decode("utf-8", errors="replace")
    result["resize_vhd"] = "resize-vhd=True" in out
    result["get_vhd"] = "get-vhd=True" in out
    result["available"] = bool(result["resize_vhd"])
    if not result["available"]:
        result["detail"] = ("Hyper-V PowerShell module absent: Resize-VHD "
                            "is not available")
    _TOOLCHAIN_CACHE["toolchain"] = dict(result)
    return result


def attached_state(container_path, toolchain=None):
    """Whether the container is currently attached/loaded, or unknown.

    ``loaded`` is False only when a read-only virtdisk handle positively says
    the disk is not loaded; a probe that could not answer leaves it None, and
    the grow path fails closed on None.
    """
    state = {"loaded": None, "attached_to": None, "source": None, "detail": None}
    if sa_win is None:
        state["detail"] = "sa_win/virtdisk unavailable"
        return state
    try:
        info = sa_win.virtual_disk_info(container_path)
    except Exception as exc:
        state["detail"] = str(exc)
        return state
    state["loaded"] = bool(info.get("is_loaded"))
    state["source"] = "virtdisk:GetVirtualDiskInformation(IS_LOADED)"
    return state


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------
def resize_vhd_command(new_gb):
    """The exact documented file-oriented resize invocation (never run here)."""
    return ("Resize-VHD -Path <container> -SizeBytes %d"
            % (int(new_gb) * 1024 ** 3))


def grow_procedure():
    """The ordered procedure a grow follows, for display before any decision.

    Advisory and always available, so the GUI can show the exact proposed
    change, its safety requirements and its post-operation checks even when
    the plan itself refuses.
    """
    return [
        {"step": "verify_idle",
         "text": "Confirm the vault is idle: no broker session mounted, no "
                 "application running from the vault, no Explorer window open "
                 "inside it. The operator states this; the product does not "
                 "guess it.",
         "requires": "operator", "consent": True},
        {"step": "confirm_detached",
         "text": "Confirm the container is not attached/loaded anywhere "
                 "(virtdisk IS_LOADED must be False).",
         "requires": "admin"},
        {"step": "resize_vhd",
         "text": "Resize-VHD -Path <container> -SizeBytes <new> (the "
                 "documented file-oriented VHDX resize; Hyper-V PowerShell "
                 "module required).",
         "requires": "admin"},
        {"step": "verify_disk_size",
         "text": "Re-read the VHDX VirtualSize and require it to equal the "
                 "target before touching the filesystem.",
         "requires": "admin"},
        {"step": "unlock_and_extend_fs",
         "text": "Unlock the volume with a normal key and extend the "
                 "filesystem inside to fill the enlarged disk.",
         "requires": "admin"},
        {"step": "verify",
         "text": "Re-read capacity (logical size equals the target) and "
                 "confirm BitLocker still reports protection ON.",
         "requires": "admin"},
    ]


def grow_checks(container_path, provisioned_gb, new_gb, facts=None,
                toolchain=None, attached=None):
    """Every precondition, in evaluation order, as ``{code, ok, detail}``.

    Order is part of the contract: argument sanity is judged before the
    container is even looked at, so an invalid size is reported as an invalid
    size rather than as whatever check happened to run last.
    """
    toolchain = toolchain if toolchain is not None else hyperv_toolchain()
    checks = []

    def add(code, ok, detail):
        checks.append({"code": code, "ok": bool(ok), "detail": detail})
        return ok

    if not add("target_is_integer",
               isinstance(new_gb, int) and not isinstance(new_gb, bool),
               "requested size %r must be a whole number of GiB" % (new_gb,)):
        return checks
    if not add("target_positive", new_gb > 0,
               "requested size must be greater than zero GiB"):
        return checks
    if not add("target_within_vhdx_max", new_gb <= MAX_GROW_GB,
               "requested size %d GiB exceeds the %d GiB VHDX maximum"
               % (new_gb, MAX_GROW_GB)):
        return checks
    if not add("provisioned_known", provisioned_gb is not None,
               "registry has no provisioned size for this container"):
        return checks
    if not add("provisioned_sane",
               isinstance(provisioned_gb, int)
               and not isinstance(provisioned_gb, bool) and provisioned_gb > 0,
               "provisioned size must be a positive whole number of GiB"):
        return checks
    if not add("container_path_present", bool(container_path),
               "profile has no container path"):
        return checks

    facts = facts if facts is not None else container_facts(container_path,
                                                             provisioned_gb)
    if not add("container_exists", facts.get("exists"),
               "container not found: %s" % container_path):
        return checks
    if not add("container_is_valid_vhdx", facts.get("format_valid") is not False,
               "container is not a valid VHDX/VHD image: %s" % container_path):
        return checks
    capacity = facts.get("virtual_capacity_gb")
    if not add("virtual_capacity_measured", capacity is not None,
               "VHDX virtual capacity could not be measured; refusing to grow "
               "a container whose current capacity is unknown"):
        return checks
    if not add("target_exceeds_measured_capacity", new_gb > capacity,
               "new size %d GiB must exceed the measured virtual capacity "
               "%.2f GiB" % (new_gb, capacity)):
        return checks
    if not add("target_exceeds_provisioned", new_gb > provisioned_gb,
               "new size %d GiB must exceed provisioned %d GiB"
               % (new_gb, provisioned_gb)):
        return checks
    delta = new_gb - capacity
    if not add("delta_is_meaningful", delta >= MIN_GROW_DELTA_GB,
               "growth delta %.2f GiB is below the %.0f GiB minimum"
               % (delta, MIN_GROW_DELTA_GB)):
        return checks
    free = facts.get("host_free_gb")
    if not add("host_free_verified", free is not None,
               "host volume free space could not be verified; unknown is not "
               "the same as sufficient, so growth is refused"):
        return checks
    if not add("host_has_room", free >= delta + GROW_MIN_FREE_GB,
               "host volume has %.2f GiB free; growing needs the %.2f GiB "
               "delta plus %.0f GiB margin"
               % (free, delta, GROW_MIN_FREE_GB)):
        return checks
    attached = attached if attached is not None else attached_state(
        container_path, toolchain)
    if not add("container_detached", attached.get("loaded") is False,
               "container is attached/loaded (or its state could not be "
               "verified); detach it and re-check"):
        return checks
    if not add("toolchain_available", toolchain.get("available"),
               "the documented Resize-VHD operation is not available on this "
               "host (%s); no grow is attempted and no Hyper-V feature is "
               "installed automatically"
               % (toolchain.get("detail") or "Hyper-V PowerShell module absent")):
        return checks
    return checks


def plan_grow(container_path, provisioned_gb, new_gb, facts=None,
              toolchain=None, attached=None):
    """A grow plan, or the first reason it is refused -- exactly one is truthy.

    Nothing is ever mutated here. The plan names the real operation, the
    measured delta, and the post-operation checks; the refusal is the earliest
    failing precondition with its own words.
    """
    if toolchain is None:
        toolchain = hyperv_toolchain()
    if facts is None:
        facts = container_facts(container_path, provisioned_gb)
    checks = grow_checks(container_path, provisioned_gb, new_gb, facts=facts,
                         toolchain=toolchain, attached=attached)
    failed = [c for c in checks if not c["ok"]]
    if failed:
        return None, failed[0]["detail"]
    capacity = facts["virtual_capacity_gb"]
    delta_gb = new_gb - capacity
    free = facts.get("host_free_gb")
    plan = {
        "container": container_path,
        "provisioned_gb": provisioned_gb,
        "measured_capacity_gb": capacity,
        "new_gb": new_gb,
        "delta_gb": round(delta_gb, 2),
        "host_free_gb": free,
        "command": resize_vhd_command(new_gb),
        "toolchain": toolchain,
        "checks": checks,
        "steps": grow_procedure(),
        "executable": True,
        "consent_required": True,
        "refusal_if_midway_fails": (
            "any failed stage stops the sequence and leaves the container in "
            "place: the filesystem is extended only after the virtual disk "
            "resize has been verified, so an interruption widens the "
            "container but never eats data"),
    }
    return plan, None


# --------------------------------------------------------------------------
# Wave C: the consent-gated execution path
# --------------------------------------------------------------------------
def _default_runner(script, timeout=900):
    """Run one PowerShell step; never raise, always return a truthful verdict."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        return RETURN_UNKNOWN, "", ["powershell.exe not found"]
    except subprocess.TimeoutExpired:
        return RETURN_UNKNOWN, "", ["PowerShell timed out"]
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    err = [line.strip() for line in
           proc.stderr.decode("utf-8", errors="replace").splitlines()
           if line.strip()][-3:]
    if proc.returncode == 0:
        return RETURN_OK, out, err
    return RETURN_REFUSED, out, err


class GrowJournal(object):
    """Append-only record of a grow attempt, so a crash is never a mystery."""

    def __init__(self, path=None):
        self.path = path
        self.records = []

    def write(self, stage, status, detail=None, **extra):
        record = {"at": datetime.datetime.now().replace(
            microsecond=0).isoformat(sep=" "),
            "stage": stage, "status": status, "detail": detail}
        record.update(extra)
        self.records.append(record)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, default=str) + "\n")
            except OSError:
                pass
        return record


def execute_grow(container_path, provisioned_gb, new_gb, *, consent=False,
                 idle_confirmed=False, journal_path=None, runner=None,
                 broker_locked=None, facts=None, toolchain=None, attached=None):
    """Grow a container's virtual size -- only on a fully passed plan.

    Refusals, in order and none of them optional:

      * no consent, or the operator has not confirmed the vault is idle;
      * anything :func:`plan_grow` refuses (which is everything unsafe);
      * a broker that is not positively locked.

    Every stage is verified before the next runs, each stage is journalled,
    and a stage that cannot be verified stops the sequence with an honest
    partial-completion report. There is no automatic fallback, no silent
    filesystem touch, and no unattended mode.
    """
    runner = runner or _default_runner
    journal = GrowJournal(journal_path)
    result = {"ok": False, "status": RETURN_REFUSED, "stage": None,
              "container": container_path, "new_gb": new_gb,
              "journal": journal.records, "stages": [],
              "capacity_before_gb": None, "capacity_after_gb": None,
              "bitlocker_protection": None, "detail": None,
              "recovery": None}

    if not consent:
        result["detail"] = ("growth requires explicit operator consent; none "
                            "was given")
        journal.write("consent", "refused", result["detail"])
        return result
    if not idle_confirmed:
        result["detail"] = ("growth requires the operator to confirm the vault "
                            "is idle (no session, no running application)")
        journal.write("verify_idle", "refused", result["detail"])
        return result
    result["stage"] = "plan"
    plan, refusal = plan_grow(container_path, provisioned_gb, new_gb,
                              facts=facts, toolchain=toolchain,
                              attached=attached)
    if plan is None:
        result["detail"] = refusal
        result["status"] = RETURN_BLOCKED
        journal.write("plan", "refused", refusal)
        return result
    result["capacity_before_gb"] = plan["measured_capacity_gb"]
    journal.write("plan", "ok", plan["command"],
                  capacity_gb=plan["measured_capacity_gb"])

    if broker_locked is None:
        result["detail"] = ("broker mount state could not be verified; growth "
                            "refuses to run without proof that no session is "
                            "mounted")
        result["status"] = RETURN_BLOCKED
        journal.write("exclusive_admission", "refused", result["detail"])
        return result
    if broker_locked is not True:
        result["detail"] = "a broker session is mounted; lock it before growing"
        journal.write("exclusive_admission", "refused", result["detail"])
        return result
    journal.write("exclusive_admission", "ok", "no session mounted")

    def stage(name, status, detail, ok=True):
        result["stage"] = name
        result["stages"].append({"stage": name, "status": status,
                                 "detail": detail})
        journal.write(name, status, detail)
        if not ok:
            result["ok"] = False
            result["detail"] = detail
            result["recovery"] = (
                "No further stage ran. Re-read capacity with 'capacity "
                "<profile>' before retrying; if the virtual disk resize "
                "succeeded but the filesystem was not extended, the volume "
                "still mounts with its old filesystem size and can be "
                "extended later.")
        return ok

    script = ("Resize-VHD -Path '%s' -SizeBytes %d"
              % (container_path.replace("'", "''"), int(new_gb) * 1024 ** 3))
    status, out, err = runner(script)
    if status != RETURN_OK:
        stage("resize_vhd", status, " ".join(err) or out or "Resize-VHD failed",
              ok=False)
        result["status"] = status if status in (RETURN_REFUSED, RETURN_UNKNOWN) \
            else RETURN_UNKNOWN
        return result
    stage("resize_vhd", "ok", script)

    try:
        after = sa_win.virtual_disk_info(container_path)["virtual_size_bytes"]
    except Exception as exc:
        stage("verify_disk_size", RETURN_UNKNOWN,
              "could not re-read VHDX size: %s" % exc, ok=False)
        result["status"] = RETURN_UNKNOWN
        return result
    result["capacity_after_gb"] = round(_gb(after), 2)
    if round(_gb(after), 2) != float(new_gb):
        stage("verify_disk_size", "mismatch",
              "VHDX reports %.2f GiB after resize; expected %d GiB"
              % (_gb(after), new_gb), ok=False)
        result["status"] = RETURN_UNKNOWN
        return result
    stage("verify_disk_size", "ok", "VHDX VirtualSize is %d GiB" % new_gb)

    stage("unlock_and_extend_fs", "operator_required",
          "the filesystem inside must now be extended with the volume "
          "unlocked (an ordinary key); the product does not extend a "
          "filesystem it cannot first verify", ok=True)
    result["ok"] = True
    result["status"] = RETURN_OK
    result["detail"] = ("virtual disk resized and verified; filesystem "
                        "extension remains an operator step")
    result["recovery"] = (
        "If the operation was interrupted before the filesystem extension, "
        "mount the volume normally and extend the filesystem to fill the "
        "enlarged disk; no data is at risk from a partial grow.")
    return result


__all__ = [
    "CapacityError", "WARN_USED_RATIO", "WARN_MIN_FREE_GB", "GROW_MIN_FREE_GB",
    "MAX_GROW_GB", "MIN_GROW_DELTA_GB", "RETURN_OK", "RETURN_REFUSED",
    "RETURN_BLOCKED", "RETURN_UNKNOWN",
    "host_volume_of", "image_magic", "container_facts", "classify",
    "hyperv_toolchain", "attached_state", "plan_grow", "grow_checks",
    "grow_procedure", "resize_vhd_command", "execute_grow", "GrowJournal",
]
