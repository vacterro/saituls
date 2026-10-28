# encoding: utf-8
"""Checks for the LIVE D/E elevation machinery.

These run at medium integrity and never touch BitLocker, a real vault or UAC.
They cover the three things that would otherwise first be discovered during the
live run, at the operator's expense:

* the parent's pipe transport (``CreateFileW`` + 4-byte big-endian framing)
  round-trips against a ``NamedPipeServerStream`` built with the same
  user-only DACL the child uses;
* a child that reports the wrong canary digest is never recorded as a pass;
* the child refuses to run at medium integrity, so no unelevated process can
  produce a D/E verdict.
"""

from __future__ import annotations

import ctypes
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from sa_live_elevation import (  # noqa: E402
    ELEVATION_PROMPT,
    NativeRecoveryWorker,
    _find_powershell,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only elevation path")

_CHILD_SCRIPT = Path(__file__).with_name("sa_native_recovery.ps1")

# A non-elevated stand-in server: same DACL and same constructor as the child,
# but it only echoes the parent's frames. It is launched exactly the way the
# child is (``-File``), because that is the shape being verified.
_PROBE_SCRIPT = r"""
param(
    [Parameter(Mandatory = $true)][string]$PipeName,
    [string]$Canary = "",
    [string]$DigestOverride = ""
)
$ErrorActionPreference = "Stop"
$id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$sec = New-Object System.IO.Pipes.PipeSecurity
$sec.AddAccessRule((New-Object System.IO.Pipes.PipeAccessRule(
    $id.User,
    [System.IO.Pipes.PipeAccessRights]::ReadWrite,
    [System.Security.AccessControl.AccessControlType]::Allow)))
$sec.SetAccessRuleProtection($true, $false)
$s = [System.IO.Pipes.NamedPipeServerStream]::new(
    $PipeName,
    [System.IO.Pipes.PipeDirection]::InOut,
    1,
    [System.IO.Pipes.PipeTransmissionMode]::Byte,
    [System.IO.Pipes.PipeOptions]::None,
    4096, 4096, $sec)
if (-not $s.WaitForConnectionAsync().Wait(60000)) { exit 9 }

function Write-Frame([string]$Text) {
    $b = [Text.Encoding]::UTF8.GetBytes($Text)
    $h = [BitConverter]::GetBytes([int]$b.Length)
    if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($h) }
    $s.Write($h, 0, 4); $s.Write($b, 0, $b.Length); $s.Flush()
}
function Read-Frame {
    $h = New-Object byte[] 4; $r = 0
    while ($r -lt 4) { $n = $s.Read($h, $r, 4 - $r); if ($n -le 0) { return $null }; $r += $n }
    if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($h) }
    $l = [BitConverter]::ToInt32($h, 0)
    $b = New-Object byte[] $l; $r = 0
    while ($r -lt $l) { $n = $s.Read($b, $r, $l - $r); if ($n -le 0) { return $null }; $r += $n }
    return [Text.Encoding]::UTF8.GetString($b)
}

Write-Frame (ConvertTo-Json -Compress @{
    type = 'hello'; pid = $PID; elevated = $false; user = $id.Name; protocol = 1 })
$req = (Read-Frame) | ConvertFrom-Json
$raw = [Convert]::FromBase64String([string]$req.secret_b64)
$sha = [System.Security.Cryptography.SHA256]::Create()
try { $digest = -join ($sha.ComputeHash($raw) | ForEach-Object { "{0:x2}" -f $_ }) }
finally { $sha.Dispose() }
if ($DigestOverride) { $digest = $DigestOverride }
Write-Frame (ConvertTo-Json -Compress @{
    type = 'result'; door = $req.door; status = 'PASS'; canary_sha256 = $Canary
    detach_confirmed = $true; secret_sha256 = $digest; secret_length = $raw.Length
    detail = '' })
$null = Read-Frame
$s.Dispose()
exit 0
"""


def _is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - non-Windows
        return False


def _start_probe(tmp_path, canary: str, expected: str | None = None, digest_override: str = ""):
    """Launch the stand-in server and return (proc, worker) wired for each other.

    ``canary`` is what the stand-in reports; ``expected`` is what the parent
    believes, so passing them differently drives the mismatch branch.
    ``digest_override`` makes the stand-in claim it received different secret
    bytes, which drives the transport-integrity branch.
    """
    pipe_name = "sa_live_test_" + secrets.token_hex(8)
    script = tmp_path / "probe_server.ps1"
    script.write_text(_PROBE_SCRIPT, encoding="utf-8-sig")

    proc = subprocess.Popen(
        [
            _find_powershell(), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-PipeName", pipe_name, "-Canary", canary, "-DigestOverride", digest_override,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker = NativeRecoveryWorker(
        # Never opened by the transport checks: only start() touches the vault.
        vault_path=str(Path(__file__)),
        expected_canary_sha256=expected or canary,
        connect_timeout_s=60.0,
    )
    worker.pipe_name = pipe_name
    worker.liveness = lambda: proc.poll() is not None
    return proc, worker


def _drain(proc, worker) -> None:
    worker.close(timeout_s=10.0)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic aid
        proc.kill()


def test_module_locates_child_and_powershell():
    assert _CHILD_SCRIPT.exists(), "tests/sa_native_recovery.ps1 is the elevated child"
    assert Path(_find_powershell()).exists()
    assert ELEVATION_PROMPT == "Approve the UAC prompt now."


def test_pipe_transport_round_trips_over_user_only_dacl(tmp_path):
    """The parent half of the protocol, exercised against a real pipe."""
    canary = "deadbeef" * 8
    proc, worker = _start_probe(tmp_path, canary)
    try:
        worker._fd = worker._connect()
        hello = worker._read_frame(timeout=60.0)
        assert hello["type"] == "hello"
        assert hello["elevated"] is False
        worker.child_pid = hello["pid"]

        result = worker.door("Password", "disposable-secret")
        assert result["status"] == "PASS", result
        assert result["canary_sha256"] == canary
        assert result["detach_confirmed"] is True
        assert result["secret_digest_matches"] is True
        assert result["secret_length"] == len("disposable-secret")
    finally:
        _drain(proc, worker)

    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode == 0, f"probe failed: {stdout}\n{stderr}"


def test_parent_detects_secret_corruption_in_transit(tmp_path):
    """A child that received different bytes must not be reported as a pass.

    Without this the pipe itself would be the one component never checked: a
    corrupted secret looks exactly like a genuine wrong-key rejection.
    """
    proc, worker = _start_probe(tmp_path, "deadbeef" * 8, digest_override="00" * 32)
    try:
        worker._fd = worker._connect()
        worker._read_frame(timeout=60.0)
        result = worker.door("Password", "disposable-secret")
        assert result["status"] == "FAIL"
        assert result["secret_digest_matches"] is False
        assert "different secret" in result["detail"]
    finally:
        _drain(proc, worker)


def test_parent_detects_canary_mismatch(tmp_path):
    """A child that reports the wrong digest must not be recorded as a pass."""
    proc, worker = _start_probe(tmp_path, "bb" * 32, expected="aa" * 32)
    try:
        worker._fd = worker._connect()
        worker._read_frame(timeout=60.0)
        result = worker.door("RecoveryPassword", "disposable-secret")
        assert result["status"] == "FAIL"
        assert "canary digest mismatch" in result["detail"]
    finally:
        _drain(proc, worker)


@pytest.mark.skipif(_is_elevated(), reason="only meaningful at medium integrity")
def test_child_refuses_medium_integrity(tmp_path):
    """No unelevated process may produce a D/E verdict."""
    vault = tmp_path / "never_opened.svault"
    vault.write_bytes(b"not a vault")
    proc = subprocess.run(
        [
            _find_powershell(), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(_CHILD_SCRIPT),
            "-PipeName", "sa_live_test_refused_" + secrets.token_hex(4),
            "-VaultPath", str(vault),
            "-ExpectedCanarySha256", "00" * 32,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 87, f"expected refusal, got {proc.returncode}"
    assert "NOT_ELEVATED" in proc.stderr
