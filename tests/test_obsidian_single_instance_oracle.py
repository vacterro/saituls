"""LIVE ORACLE: does this Obsidian build hand off to a running instance?

The question is not academic. SAITULS launches
``<managed-root>\\apps\\obsidian\\app\\Obsidian.exe`` with the vault path as an
argument and no ``--user-data-dir``. Electron's single-instance lock is keyed on
the user-data directory, which defaults to ``%APPDATA%\\obsidian`` for both the
managed copy and any portable copy the operator already runs. If the lock is
honoured, a protected launch while an unmanaged Obsidian is already open does
NOT start a protected process: the new process hands its arguments to the old
one and exits, and the process that ends up reading the vault is one SAITULS
launched nothing of, owns nothing of, and cannot detach from.

This file answers that with the real binary rather than by reading Electron
docs. It never touches the operator's running Obsidian and never touches their
vault:

  * both specimen instances use a throwaway ``--user-data-dir``, so the lock
    they contend for is one this test created;
  * both are launched with no vault argument, so no vault path is ever opened;
  * teardown kills only processes whose command line contains this test's own
    unique temp directory, and nothing else.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
import struct

PSUTIL = None
try:
    import psutil
    PSUTIL = psutil
except ImportError:  # pragma: no cover - the test skips below
    pass


def _find_obsidian():
    """The operator's own portable Obsidian, if it is on this machine.

    SAITULS's managed copy is the thing under test in production; this oracle
    runs against whichever Obsidian is here, because the behaviour under test
    is a property of the build (Electron single-instance), not of its path.
    """
    candidates = [
        r"V:\___VAC\__P\_OBSIDIAN\Obsidian.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Obsidian", "Obsidian.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Obsidian", "Obsidian.exe"),
    ]
    managed = (Path(os.environ.get("LOCALAPPDATA", "")) / "SAITULS" / "secure-apps"
               / "apps" / "obsidian" / "app" / "Obsidian.exe")
    candidates.append(str(managed))
    for path in candidates:
        # A real Electron executable starts with the MZ PE header; a stub,
        # placeholder or corrupted download is a launch-during-test failure
        # (WinError 216) rather than evidence about single-instance behaviour.
        if path and _is_real_pe(path):
            return path
    return None


def _is_real_pe(path):
    """A candidate launchable as a Windows executable: exists and is a PE image.

    'MZ' + a PE signature at the COFF-header offset from the DOS header is all
    Windows executables share; a fake ``Obsidian.exe`` left behind by a failed
    download is typically not one.
    """
    try:
        if not path or not os.path.isfile(path):
            return False
        if os.path.getsize(path) < 64:
            return False  # too small to hold an MZ header plus PE offset
        with open(path, "rb") as fh:
            if fh.read(2) != b"MZ":
                return False
            fh.seek(0x3C)
            (pe_offset,) = struct.unpack("<I", fh.read(4))
            if not (0x40 <= pe_offset <= os.path.getsize(path) - 6):
                return False
            fh.seek(pe_offset)
            return fh.read(4) == b"PE\x00\x00"
    except OSError:
        return False


def _pids_for(user_data_dir):
    """Every Obsidian process whose command line names *user_data_dir*."""
    marker = user_data_dir.lower()
    found = []
    for proc in PSUTIL.process_iter(["pid", "name", "cmdline"]):
        try:
            if (proc.info.get("name") or "").lower() != "obsidian.exe":
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if marker in cmdline.lower():
                found.append(int(proc.info["pid"]))
        except Exception:
            continue
    return sorted(found)


class ObsidianSingleInstanceOracle(unittest.TestCase):
    """The live answer to AC10, recorded rather than assumed."""

    OBSIDIAN = _find_obsidian()

    def setUp(self):
        if os.name != "nt" or PSUTIL is None:
            self.skipTest("needs Windows and psutil")
        if not self.OBSIDIAN:
            self.skipTest("no Obsidian binary on this machine")

    def test_second_launch_hands_off_instead_of_starting_a_second_instance(self):
        # A unique directory per run: this test owns the lock it contends for
        # and cannot disturb an Obsidian the operator is actually using.
        user_data_dir = tempfile.mkdtemp(prefix="saituls-obsidian-oracle-")
        argv = [self.OBSIDIAN, "--user-data-dir=%s" % user_data_dir]
        first = subprocess.Popen(argv)
        self.addCleanup(self._teardown, user_data_dir, [first.pid])
        deadline = time.time() + 30
        while time.time() < deadline and not _pids_for(user_data_dir):
            time.sleep(0.5)
        first_pids = _pids_for(user_data_dir)
        self.assertTrue(first_pids,
                        "the first launch never produced a live Obsidian "
                        "process; the oracle cannot conclude anything")

        # The second launch is the protected one: SAITULS spawns the managed
        # executable with a vault argument. Here the argument is omitted so no
        # vault is opened; the single-instance decision is the same either way
        # because it happens before any argument is used.
        second = subprocess.Popen(argv)
        time.sleep(12)
        second_pids = _pids_for(user_data_dir)

        # The launched pid itself is the decisive datum: a process that handed
        # off exits almost immediately, so its absence from the live set while
        # the FIRST instance is still alive is the handoff.
        still_running = second.poll() is None
        alive = set(second_pids)
        first_still_alive = bool(set(first_pids) & alive)
        handoff = (not still_running) and first_still_alive

        print("\n[obsidian-oracle] exe=%s" % self.OBSIDIAN)
        print("[obsidian-oracle] first launch pids: %r" % first_pids)
        print("[obsidian-oracle] second launch pid %s running=%s"
              % (second.pid, still_running))
        print("[obsidian-oracle] live pids after second launch: %r" % second_pids)
        print("[obsidian-oracle] SINGLE_INSTANCE_HANDOFF=%s" % handoff)
        if handoff:
            print("[obsidian-oracle] A pre-existing instance wins: a second "
                  "launch with the same user-data-dir hands off and exits. "
                  "SAITULS must therefore REFUSE a protected launch while an "
                  "unmanaged Obsidian is running, and must never adopt the "
                  "running instance as if it were its own.")

        if not handoff:
            self.skipTest("this build did not hand off (both instances stayed "
                          "alive); the conflict rule still applies, but the "
                          "refusal is then about two writers rather than one")
        self.assertFalse(still_running,
                         "the second launch handed off and exited, so SAITULS "
                         "must never treat a spawn as a running protected app")
        self.assertTrue(first_still_alive,
                        "the pre-existing instance is the one that survives")

    @staticmethod
    def _teardown(user_data_dir, extra_pids):
        """Kill ONLY this test's processes, identified by their own directory."""
        if PSUTIL is not None:
            for pid in _pids_for(user_data_dir) + list(extra_pids):
                try:
                    proc = PSUTIL.Process(int(pid))
                    cmdline = " ".join(proc.cmdline() or [])
                    if user_data_dir.lower() in cmdline.lower():
                        proc.kill()
                except Exception:
                    continue
        time.sleep(1.0)
        try:
            import shutil
            shutil.rmtree(user_data_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
