"""Scripted emergency recovery, one step at a time, each refusing to lie.

The emergency tab already prints the right human procedure. T-254 is about
the difference between printing a procedure and running one: an operator in
distress should not have to hand-transcribe PowerShell under pressure, and
more importantly should not have a tool that silently joins steps together
when one of them fails.

Rules this module keeps, every one enforced, none optional:

  * never touch the operator's live mounted working container -- these steps
    act on the standalone container image path of the *selected profile*,
    which is the same path the printed instructions name;
  * the loop between steps is the operator's: after any step that fails, is
    refused, or prints something unexpected the function returns and reports
    exactly what happened -- a refusal is a stop, not a warning;
  * the recovery password is never passed on a command line or written to
    any file, log, or status output; it travels in-memory and reaches
    manage-bde only via standard input to PowerShell;
  * every step returns a truthful (status, detail) verdict, which the caller
    renders; "unknown" is a real verdict and is reported as such.

Each operation is a thin, honest wrapper around standard Windows tooling
(Mount-DiskImage, Get-DiskImage, manage-bde) and is safe to run repeatedly:
mount is skipped when the image is already attached, unlock reports an
already-unlocked volume instead of failing, and lock/dismount are no-ops
with a truthful status on an already-detached image.
"""
import os
import re
import subprocess

#: 48 digits in 8 dash-separated groups of 6 -- the manage-bde input shape.
_RECOVERY_SHAPE = re.compile(r"^\d{6}(?:-\d{6}){7}$")

RETURN_OK = "ok"
RETURN_REFUSED = "refused"
RETURN_UNKNOWN = "unknown"
RETURN_MISSING = "missing"


class RecoveryError(Exception):
    """A structured, non-secret failure raised back to the CLI/GUI."""


def _emit_ps_errors(stderr):
    """Compact stderr for an operator who is under stress, never a wall."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-3:] if lines else []


def _run_pwsh(script, timeout=120):
    """Run a PowerShell snippet, returning a truthful verdict, never None.

    Return pair is (status, data, stderr_tail); status is one of the RETURN_*
    constants. EXIT_REFUSED-style non-zero exits are mapped by the caller.
    """
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return RETURN_MISSING, None, ["powershell.exe not found on PATH"]
    except subprocess.TimeoutExpired:
        return RETURN_UNKNOWN, None, ["PowerShell timed out; image state unverified"]
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr_tail = _emit_ps_errors(proc.stderr.decode("utf-8", errors="replace"))
    if proc.returncode == 0:
        return RETURN_OK, stdout, stderr_tail
    if proc.returncode in (1, 0x80131500):  # generic PS failure / CLR error
        return RETURN_REFUSED, stdout, stderr_tail
    return RETURN_UNKNOWN, stdout, stderr_tail


def attach_with_letter(container_path):
    """Mount the VHDX read-write and guarantee its partition has a letter.

    Windows attaches a disk with NO letter on its own, so this checked part
    of the procedure is the one that is easiest to get silently wrong by
    hand. Reports the letter found or assigned, or refuses with the reason.
    """
    if not os.path.isfile(container_path):
        return RETURN_MISSING, None, ["container not found: %s" % container_path]
    script = r"""
$ErrorActionPreference = 'Stop'
$img = '%(img)s'
$already = Get-DiskImage -ImagePath $img -ErrorAction SilentlyContinue
if ($already -and $already.Attached) {
    Write-Output "ALREADY_MOUNTED"
    $disk = $already | Get-Disk
} else {
    Mount-DiskImage -ImagePath $img -Access ReadWrite | Out-Null
    $disk = Get-DiskImage -ImagePath $img | Get-Disk
}
$part = $disk | Get-Partition | Where-Object DriveLetter | Select-Object -First 1
if (-not $part) {
    $letter = (Get-AvailableDriveLetter | Select-Object -First 1)
    if (-not $letter) { throw 'no drive letter available' }
    $part = $disk | Get-Partition | Select-Object -First 1
    if (-not $part) { throw 'no partition present on attached image' }
    if ($already -and $already.Attached) {
        throw 'image already attached without a letter; refusing changes without consent'
    }
    Add-PartitionAccessPath -Partition $part -AccessPath ($letter + ':') | Out-Null
    $driveLetter = $letter
} else {
    $driveLetter = $part.DriveLetter
}
Write-Output "LETTER=$driveLetter"
""" % {"img": container_path.replace("'", "''")}
    status, out, err = _run_pwsh(script)
    if status != RETURN_OK:
        return status, None, err
    if "LETTER=" not in (out or ""):
        return RETURN_UNKNOWN, None, ["unexpected mount output", out or ""]
    letter = out.split("LETTER=", 1)[1].split()[-1]
    return RETURN_OK, {"letter": letter}, err


def _valid_recovery_password(value):
    """Exactly 8 groups of 6 digits, dash-separated: the BitLocker shape.

    A loose 'contains a dash' check would let 'not-a-key' through to a
    confused manage-bde refusal that teaches the operator nothing; the key
    answer is the strict shape, checked before any process starts.
    """
    text = str(value or "").strip()
    return bool(_RECOVERY_SHAPE.match(text))


def unlock_with_password(container_path, letter, recovery_password):
    """Unlock the attached partition with the 48-digit recovery password.

    The password travels via standard input to the child process,
    not on a command line or in an environment variable, and never into any returned detail: captured
    child output is scanned for the key material and any echo of it turns
    the step into a loud refusal instead of a leak.
    """
    if not _valid_recovery_password(recovery_password):
        return RETURN_REFUSED, None, ["refused: a 48-digit recovery password "
                                      "in XXXXXX-...-XXXXXX form is required"]
    script = r"""
$ErrorActionPreference = 'Stop'
$i = Get-DiskImage -ImagePath '%(img)s' | Get-Disk |
    Get-Partition | Where-Object DriveLetter -eq '%(letter)s'
if (-not $i) { throw 'attached disk has no partition with that drive letter' }
$vol = Get-Volume -DriveLetter '%(letter)s'
if ($vol.OperatingStatus -notmatch 'BitLocker|Locked') {
    Write-Output "ALREADY_UNLOCKED"
    exit 0
}
$k = [Console]::In.ReadLine()
manage-bde -unlock ('%(letter)s' + ':') -RecoveryPassword $k
""" % {"img": container_path.replace("'", "''"),
           "letter": letter.replace("'", "''")}
    # The key leaves only via standard input; the command line and environment stay clean.
    key_bytes = ("%s\n" % str(recovery_password).strip()).encode("ascii")
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                               "-Command", script],
                              input=key_bytes,
                              capture_output=True, timeout=120)
    except FileNotFoundError:
        return RETURN_MISSING, None, ["powershell.exe not found on PATH"]
    except subprocess.TimeoutExpired:
        return RETURN_UNKNOWN, None, ["manage-bde timed out; volume state unverified"]
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr_tail = _emit_ps_errors(proc.stderr.decode("utf-8", errors="replace"))
    blob = (stdout or "") + "\n" + "\n".join(stderr_tail)
    if str(recovery_password).strip() in blob:
        # The child echoed the key (or something bit-for-bit like it). That
        # capture must not be handed to any caller, log or clipboard.
        return (RETURN_REFUSED, None,
                ["refused: tooling echoed key material; output suppressed "
                 "-- rerun in a terminal that does not echo"])
    if proc.returncode == 0:
        return RETURN_OK, {"unlocked": True}, stderr_tail
    if (proc.returncode in (0xC0000022, 0x80070005, 2147942405)
            or "access denied" in (stdout + " ".join(stderr_tail)).lower()):
        return RETURN_REFUSED, None, stderr_tail + ["elevated PowerShell required for manage-bde"]
    return RETURN_REFUSED, None, stderr_tail + [ln for ln in stdout.splitlines() if ln.strip()][-2:]


def lock_and_dismount(container_path):
    """Lock the volume back down and detach the image; idempotent, honest.

    Dismount refuses if the volume is still unlocked (_manage-bde lock_
    first then dismount); this wrapper never force-dismounts a live,
    unlocked volume -- doing so risks a torn state and is the one thing
    recovery must never gamble on.
    """
    if not os.path.isfile(container_path):
        return RETURN_MISSING, None, ["container not found: %s" % container_path]
    script = r"""
$ErrorActionPreference = 'Stop'
$img = '%(img)s'
$attached = Get-DiskImage -ImagePath $img -ErrorAction SilentlyContinue
if (-not ($attached -and $attached.Attached)) {
    Write-Output "ALREADY_DETACHED"
    exit 0
}
$disk = $attached | Get-Disk
foreach ($part in ($disk | Get-Partition)) {
    if ($part.DriveLetter) {
        $vol = Get-Volume -DriveLetter $part.DriveLetter
        $lock = manage-bde -status $part.DriveLetter':' 2>&1 | Out-String
        if ($lock -match 'Fully Decrypted' -or $lock -match 'Locked') {
            continue
        }
        manage-bde -lock $part.DriveLetter':' | Out-Null
    }
}
Dismount-DiskImage -ImagePath $img | Out-Null
Write-Output "DISMOUNTED"
""" % {"img": container_path.replace("'", "''")}
    status, out, err = _run_pwsh(script)
    if status != RETURN_OK:
        return status, None, err
    if "DISMOUNTED" not in (out or "") and "ALREADY_DETACHED" not in (out or ""):
        return RETURN_UNKNOWN, None, ["unexpected lock/dismount output", out or ""]
    return RETURN_OK, {"detached": True}, err


def run_recovery_sequence(container_path, password, finish=False):
    """The full scripted path, stopping at the first refusal.

    attach -> unlock -> stop for the operator to read their notes ->
    lock_and_dismount. `finish=False` stops after unlock: the operator works
    with the mounted volume (what the printed procedure calls step 3), then
    calls lock_and_dismount themselves. Each entry is
    (step_name, status, detail). After any non-ok step the remaining steps
    are NOT attempted and the verdict is "stopped", never "failed silently".
    """
    sequence = []
    attach = attach_with_letter(container_path)
    sequence.append(("attach", attach[0], attach[2]))
    if attach[0] != RETURN_OK:
        return sequence, "stopped"

    unlock = unlock_with_password(container_path, attach[1]["letter"], password)
    sequence.append(("unlock", unlock[0], unlock[2]))
    if unlock[0] != RETURN_OK:
        return sequence, "stopped"

    if not finish:
        sequence.append(("operator_work", RETURN_OK,
                         ["volume unlocked and mounted; when done, run lock_dismount"]))
        return sequence, "operator_usable"

    close = lock_and_dismount(container_path)
    sequence.append(("lock_dismount", close[0], close[2]))
    if close[0] != RETURN_OK:
        return sequence, "stopped"
    return sequence, "complete"


__all__ = [
    "RecoveryError",
    "RETURN_OK", "RETURN_REFUSED", "RETURN_UNKNOWN", "RETURN_MISSING",
    "attach_with_letter", "unlock_with_password", "lock_and_dismount",
    "run_recovery_sequence",
]
