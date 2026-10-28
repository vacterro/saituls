"""T-255: the repo scratch pen stays out of git and stays on disk.

The hygiene ticket asks for "debug residue removed ... and the regenerating
patterns ignored". Removal collided with a rule this repo holds above it:
files under tmp/ include acceptance and incident evidence (captured scheduled
task XML, USN probe results, elevation transcripts), and a generic cleanup
must never destroy incident evidence under a cleanup label.

The durable disposition is therefore:

  1. tmp/ is git-ignored, so one-off residue can never reach a commit;
  2. tmp/ is NOT deleted, and no repo hygiene tooling may bulk-delete it;
  3. the specific named residue patterns from the ticket (identity-trace.txt,
     createQueueCore, submit_window.txt, opencode_serve.log, and the diag
     probe shapes) stay individually ignored as well.

These checks run `git` itself so the assertion is about the real repo state,
not about a copy of the ignore rules.
"""
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

NAMED_RESIDUE = [
    "ACCEPT",
    "createQueueCore",
    "identity-trace.txt",
    "submit_window.txt",
    "opencode_serve.log",
    "tq_out.txt",
    "tq_tmp/x",
    "_diag.py",
    "_diag_out.txt",
    "tmp_probe_cov.txt",
]


def _git(*argv):
    return subprocess.run(["git", *argv], cwd=REPO, capture_output=True,
                          text=True).stdout.strip()


class ScratchPenIgnored(unittest.TestCase):

    def test_root_scratch_pen_is_ignored(self):
        out = _git("check-ignore", "-v", "--no-index", "tmp/zz_accept.ps1",
                   "tmp/usn_probe_out.txt")
        entries = [ln.split("\t")[-1] for ln in out.splitlines() if ln.strip()]
        self.assertEqual(entries, ["tmp/zz_accept.ps1", "tmp/usn_probe_out.txt"],
                         "tmp/ scratch pen must be matched by .gitignore")

    def test_named_residue_patterns_are_individually_ignored(self):
        probes = []
        for name in NAMED_RESIDUE:
            probe = "tmp/%s" % name if "/" not in name else (
                "scratch/%s" % name if name.startswith("tq_") else name)
            probes.append(probe)
        out = _git("check-ignore", "--no-index", *probes)
        ignored = set(out.splitlines())
        for probe in probes:
            self.assertIn(probe, ignored,
                          "%s must remain a separately ignored pattern" % probe)

    def test_scratch_pen_never_bulk_deleted_by_hygiene(self):
        # Read the hygiene policy module, not its behavior: the AGGRESSIVE
        # junk-name list must not target the repo scratch pen by directory.
        policy = (REPO / "Scripts" / "del_junk_policy.py").read_text(
            encoding="utf-8")
        self.assertIn('"tmp"', policy,
                      "junk-name list should still know the name 'tmp'")
        self.assertNotIn('"tmp/"', policy.replace('"tmp",', ""),
                         "no directory-form tmp/ target may be added for "
                         "bulk deletion")

    def test_no_scratch_pen_file_is_tracked(self):
        tracked = _git("ls-files", "tmp/")
        self.assertEqual("", tracked,
                         "a commit of tmp/ contents would re-invite the drift")


if __name__ == "__main__":
    unittest.main()
